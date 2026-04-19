"""
Semi-random QA (and engineer) selection for Plaky assignment.

- Tier + hardware: heavy repos filter out low-tier QAs when configured.
- Overlap pools: QAs who share org or explicit repo overlap form a pool; we pick within pool.
- Weights: member.weight * tier bias * uniform jitter → weighted random choice.
- Auto-classify: If repo not in repos.yml, fetch metadata and classify tier dynamically.
"""

from __future__ import annotations

import logging
import random
import re
from fnmatch import fnmatchcase
from typing import Dict, List, Optional, Set, Tuple

from boardman.assignment.config import TeamAssignmentsConfig, TeamMember, load_team_assignments
from boardman.github.repo_metadata import fetch_repo_metadata
from boardman.assignment.tier_classifier import classify_repo_tier
from boardman.repos_config import get_routing
from boardman.settings import settings

_log = logging.getLogger(__name__)


async def _auto_classify_repo_tier(full_name: str) -> int:
    """
    Auto-classify repo tier if not in repos.yml.
    Returns tier (1, 2, 3) or defaults to 2 if classification fails.
    """
    if "/" not in full_name:
        return 2
    
    owner, repo = full_name.split("/", 1)
    
    import httpx
    async with httpx.AsyncClient(timeout=30.0) as client:
        meta = await fetch_repo_metadata(client, owner, repo)
    
    if meta:
        tier, _ = classify_repo_tier(meta)
        _log.info("Auto-classified repo %s as tier %d", full_name, tier)
        return tier
    
    _log.warning("Could not fetch metadata for %s, defaulting to tier 2", full_name)
    return 2


def _norm_repo(full: str) -> str:
    return (full or "").strip().lower()


def repo_matches_member(full_name: str, m: TeamMember) -> bool:
    """True if this GitHub full name (owner/repo) is in this member's scope."""
    key = _norm_repo(full_name)
    if not key:
        return False
    for ex in m.explicit_repos:
        if ex == key:
            return True
    for g in m.repo_globs:
        g = g.strip()
        if not g:
            continue
        if fnmatchcase(key, g.lower()):
            return True
    return False


def repo_is_heavy(full_name: str, patterns: List[str]) -> bool:
    key = (full_name or "").strip().lower()
    for p in patterns:
        if fnmatchcase(key, p.lower()):
            return True
    return False


def _tier_bias(cfg: TeamAssignmentsConfig, tier_name: str) -> float:
    t = cfg.tiers.get(tier_name.lower())
    if t:
        return max(0.1, t.weight_bias)
    return 1.0


def _owners_from_member(m: TeamMember) -> Set[str]:
    owners: Set[str] = set()
    for ex in m.explicit_repos:
        if "/" in ex:
            owners.add(ex.split("/")[0])
    for g in m.repo_globs:
        if "/" in g:
            owners.add(g.split("/")[0].lower().replace("*", ""))
            m2 = re.match(r"^([^/*]+)", g)
            if m2:
                owners.add(m2.group(1).lower())
    return owners


def _explicit_overlap(a: TeamMember, b: TeamMember) -> bool:
    sa = set(a.explicit_repos)
    sb = set(b.explicit_repos)
    return bool(sa & sb)


def _same_owner_glob_overlap(a: TeamMember, b: TeamMember) -> bool:
    oa = _owners_from_member(a)
    ob = _owners_from_member(b)
    return bool(oa & ob)


def _overlap_component(
    eligible: List[TeamMember],
) -> List[TeamMember]:
    """
    Partition `eligible` into connected components by edges:
    explicit repo set intersection OR shared GitHub owner in patterns.
    Return the component that has maximum size (largest overlap pool).
    If single member, return them.
    """
    if len(eligible) <= 1:
        return eligible
    n = len(eligible)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    for i in range(n):
        for j in range(i + 1, n):
            a, b = eligible[i], eligible[j]
            if _explicit_overlap(a, b) or _same_owner_glob_overlap(a, b):
                union(i, j)

    buckets: Dict[int, List[int]] = {}
    for i in range(n):
        r = find(i)
        buckets.setdefault(r, []).append(i)

    best = max(buckets.values(), key=len)
    return [eligible[i] for i in best]


def _weighted_choice(members: List[TeamMember], cfg: TeamAssignmentsConfig) -> Optional[TeamMember]:
    if not members:
        return None
    if len(members) == 1:
        return members[0]
    jitter = cfg.random_jitter
    weights: List[float] = []
    for m in members:
        w = max(0.05, m.weight)
        w *= _tier_bias(cfg, m.tier)
        if jitter > 0:
            w *= 1.0 + random.uniform(-jitter, jitter)
        weights.append(max(0.01, w))
    s = sum(weights)
    weights = [w / s for w in weights]
    return random.choices(members, weights=weights, k=1)[0]


async def pick_qa_for_repo(full_name: str, cfg: Optional[TeamAssignmentsConfig] = None) -> Tuple[Optional[str], str]:
    """
    Returns (plaky_person_id_or_value, reason_summary).
    Uses tier from repos.yml, or auto-classifies if not found.
    """
    cfg = cfg or load_team_assignments()
    fn = (full_name or "").strip()
    if not fn:
        return None, "empty repo"

    repo_tier = 2
    routing = get_routing(fn, "", settings.github_org)
    if routing and routing.tier > 0:
        repo_tier = routing.tier
    else:
        repo_tier = await _auto_classify_repo_tier(fn)

    qas = [m for m in cfg.members if "qa" in m.roles and repo_matches_member(fn, m)]
    if not qas:
        return None, "no QA member matched repo globs"

    # Filter by QA tier (repo_tier is the tier required - QAs must have qa_tier >= repo_tier)
    qas = [m for m in qas if m.qa_tier >= repo_tier]
    if not qas:
        return None, f"no QA after tier filter (repo tier={repo_tier}, QA tiers: {[m.qa_tier for m in qas]})"

    if repo_is_heavy(fn, cfg.heavy_repo_patterns):
        qas = [m for m in qas if m.tier.lower() not in ("light", "minimal", "low")]
        if not qas:
            return None, "heavy repo: no QA after legacy hardware tier filter (light/minimal/low dropped)"

    pool = _overlap_component(qas)
    chosen = _weighted_choice(pool, cfg)
    if not chosen:
        return None, "weighted pick failed"
    return chosen.id, f"qa={chosen.display} pool_size={len(pool)} repo_tier={repo_tier}"


def pick_engineer_for_repo(full_name: str, cfg: Optional[TeamAssignmentsConfig] = None) -> Tuple[Optional[str], str]:
    """Deterministic: highest weight among matching engineers (no random)."""
    cfg = cfg or load_team_assignments()
    fn = (full_name or "").strip()
    eng = [m for m in cfg.members if "engineer" in m.roles and repo_matches_member(fn, m)]
    if not eng:
        return None, "no engineer matched"
    eng.sort(key=lambda m: (-m.weight, m.display))
    top = eng[0]
    return top.id, f"engineer={top.display}"


def github_repo_suffix_name(full: str) -> str:
    """Return repository name only (segment after last ``/``); unchanged if no slash."""
    s = (full or "").strip()
    if not s:
        return ""
    return s.rsplit("/", 1)[-1] if "/" in s else s


def _dedupe_repo_list(repos: Optional[List[str]]) -> List[str]:
    out: List[str] = []
    seen_set: set[str] = set()
    if not repos:
        return out
    for raw in repos:
        s = str(raw or "").strip()
        if not s:
            continue
        k = s.lower()
        if k in seen_set:
            continue
        seen_set.add(k)
        out.append(s)
    return out


def normalize_github_repo_inputs(
    primary_repo: str = "",
    github_repos: Optional[List[str]] = None,
    *,
    extra_repo_text: str = "",
) -> List[str]:
    """Return ordered unique owner/repo values from list and comma/newline text."""
    tokens: List[str] = []
    if primary_repo and primary_repo.strip():
        tokens.append(primary_repo)
    if isinstance(github_repos, list):
        tokens.extend(str(repo or "") for repo in github_repos)
    raw_extra = (extra_repo_text or "").strip()
    if raw_extra:
        tokens.extend(raw_extra.replace("\n", ",").split(","))
    return _dedupe_repo_list(tokens)


def _format_repo_tokens_for_plaky(tokens: List[str], fmt: str) -> List[str]:
    """``fmt`` ``short`` = repo name only (for TAG columns); ``full`` = keep ``owner/repo``."""
    if fmt != "short" or not tokens:
        return list(tokens)
    out: List[str] = []
    seen: set[str] = set()
    for t in tokens:
        sh = github_repo_suffix_name(t)
        if not sh:
            continue
        k = sh.casefold()
        if k not in seen:
            seen.add(k)
            out.append(sh)
    return out


def build_repo_field_map(
    cfg: Optional[TeamAssignmentsConfig] = None,
    *,
    repo_value: Optional[str] = None,
    github_repos: Optional[List[str]] = None,
    plaky_field_repo_key: Optional[str] = None,
    plaky_field_github_repos_key: Optional[str] = None,
    repo_value_format: str = "full",
    github_repos_value_format: str = "full",
) -> Dict[str, str]:
    """Map configured repo-related Plaky field keys to one or more GitHub repos.

    Use ``repo_value_format`` / ``github_repos_value_format`` of ``short`` for Plaky TAG columns
    (values are repo names only, e.g. ``deepiri-platform``).
    """
    cfg = cfg or load_team_assignments()
    out: Dict[str, str] = {}
    repo_key = (plaky_field_repo_key or cfg.plaky_field_repo or "").strip()
    repos_multi_key = (plaky_field_github_repos_key or cfg.plaky_field_github_repos or "").strip()
    repo_label = (repo_value or "").strip()
    tokens = _dedupe_repo_list(github_repos)
    if not tokens and repo_label:
        tokens = [repo_label]
    tok_repo = _format_repo_tokens_for_plaky(tokens, repo_value_format)
    tok_multi = _format_repo_tokens_for_plaky(tokens, github_repos_value_format)
    joined_repo = ", ".join(tok_repo) if tok_repo else ""
    joined_multi = ", ".join(tok_multi) if tok_multi else ""

    if repo_key and repos_multi_key and len(tokens) > 1:
        out[repo_key] = tok_repo[0] if tok_repo else ""
        out[repos_multi_key] = joined_multi
    elif repo_key and repos_multi_key and len(tokens) == 1:
        out[repo_key] = tok_repo[0] if tok_repo else ""
        out[repos_multi_key] = tok_multi[0] if tok_multi else ""
    elif repo_key and joined_repo:
        out[repo_key] = joined_repo
    elif repos_multi_key and joined_multi:
        out[repos_multi_key] = joined_multi
    return out


async def build_assignment_field_map(
    full_name: str,
    cfg: Optional[TeamAssignmentsConfig] = None,
    field_overrides: Optional[Dict[str, str]] = None,
    *,
    repo_value: Optional[str] = None,
    github_repos: Optional[List[str]] = None,
    plaky_field_repo_key: Optional[str] = None,
    plaky_field_github_repos_key: Optional[str] = None,
    plaky_field_engineer_key: Optional[str] = None,
    plaky_field_qa_key: Optional[str] = None,
    repo_value_format: str = "full",
    github_repos_value_format: str = "full",
) -> Dict[str, str]:
    """Map Plaky field key -> person id or repo label(s) for create/patch. Overrides win for same keys."""
    cfg = cfg or load_team_assignments()
    out: Dict[str, str] = {}
    eng_key = (plaky_field_engineer_key or cfg.plaky_field_engineer or "").strip()
    qa_key = (plaky_field_qa_key or cfg.plaky_field_qa or "").strip()
    eid, _ = pick_engineer_for_repo(full_name, cfg)
    if eid and eng_key:
        out[eng_key] = eid
    qid, _ = await pick_qa_for_repo(full_name, cfg)
    if qid and qa_key:
        out[qa_key] = qid
    out.update(
        build_repo_field_map(
            cfg,
            repo_value=repo_value if repo_value is not None else full_name,
            github_repos=github_repos,
            plaky_field_repo_key=plaky_field_repo_key,
            plaky_field_github_repos_key=plaky_field_github_repos_key,
            repo_value_format=repo_value_format,
            github_repos_value_format=github_repos_value_format,
        )
    )
    for k, v in (field_overrides or {}).items():
        ks, vs = str(k).strip(), str(v).strip()
        if ks and vs:
            out[ks] = vs
    return out
