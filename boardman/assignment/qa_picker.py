"""
GitHub-fit scored QA selection for Plaky assignment.

Hard filters first, then a real ranking:

- Tier filter: member.qa_tier >= repo tier (repos.yml or auto-classified) AND
  qa_repo_rules pattern rules (tier 1 = allowlist only, tier 2 = exclusions).
- Hardware: heavy repos drop light/minimal/low hardware-tier QAs.
- Fit score: each candidate's GitHub contribution profile (recency-decayed PRs
  authored/reviewed across the org, per-repo languages + topics) is compared to
  the target repo via cosine similarity — direct contributions to the target repo
  weigh most, then language overlap, then repo-name/topic token overlap.
- Final score = (base + fit) * configured weight * hardware bias * jitter; the
  top-ranked member wins and the reason string records the full ranking.
- Fallback: if GitHub profiles are unavailable (no PAT, rate limit, outage), the
  legacy overlap-pool weighted-random pick still assigns someone.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from fnmatch import fnmatchcase
from typing import Dict, List, Optional, Set, Tuple

from boardman.assignment.config import TeamAssignmentsConfig, TeamMember, load_team_assignments
from boardman.assignment.repo_rules import qa_tier_allows_repo
from boardman.github.repo_metadata import fetch_repo_metadata
from boardman.assignment.tier_classifier import classify_repo_tier
from boardman.repos_config import get_routing
from boardman.settings import settings

_log = logging.getLogger(__name__)

# Blend weights for the GitHub-fit score (sum to 1.0).
FIT_WEIGHT_DIRECT = 0.45   # decayed contributions to the target repo itself
FIT_WEIGHT_LANGUAGE = 0.30 # cosine over language distributions
FIT_WEIGHT_TOKENS = 0.25   # cosine over repo-name/topic/description token bags
# Every eligible member keeps a base score so a zero-fit candidate can still win
# on weight when nobody has relevant history.
FIT_BASE_SCORE = 0.15
# Give the whole scoring step a deadline; on timeout fall back to legacy picking.
FIT_SCORING_TIMEOUT_SECONDS = 45.0


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


async def _github_fit_scores(
    candidates: List[TeamMember], full_name: str
) -> Optional[Dict[str, Tuple[float, str]]]:
    """member.id -> (fit 0..1, detail) from GitHub contribution profiles vs the target repo.

    Returns None when the target repo info or every member profile is unavailable
    (no PAT / outage) so the caller can fall back to legacy picking.
    """
    from boardman.github.qa_contribution_profile import (
        cosine_similarity,
        direct_contribution_score,
        fetch_contribution_profile,
        fetch_repo_info,
    )

    if not settings.qa_github_fit_enabled or not (settings.github_pat or "").strip():
        return None

    import httpx

    # Search the owner org of the target repo — settings.github_org may be a legacy
    # alias that GitHub search rejects with HTTP 422 (org_repos has a discovery
    # fallback; the search API does not).
    search_org = full_name.split("/", 1)[0] if "/" in full_name else settings.github_org

    async with httpx.AsyncClient(timeout=30.0) as client:
        target = await fetch_repo_info(client, full_name)
        if target is None:
            return None
        target_lang = {target.language.lower(): 1.0} if target.language else {}
        target_tokens = target.tokens()

        # GitHub search dislikes concurrency (secondary rate limits) — keep it low.
        sem = asyncio.Semaphore(2)

        async def _one(m: TeamMember):
            login = (m.github_login or "").strip()
            if not login:
                return m.id, None
            async with sem:
                try:
                    # Per-member deadline: one slow/throttled login must not consume the
                    # whole scoring budget — cached members still rank.
                    profile = await asyncio.wait_for(
                        fetch_contribution_profile(client, login, search_org), timeout=20.0
                    )
                except asyncio.TimeoutError:
                    profile = None
            if profile is None:
                return m.id, None
            direct = direct_contribution_score(profile, full_name)
            lang_cos = cosine_similarity(profile.language_weights, target_lang)
            tok_cos = cosine_similarity(profile.token_weights, target_tokens)
            fit = FIT_WEIGHT_DIRECT * direct + FIT_WEIGHT_LANGUAGE * lang_cos + FIT_WEIGHT_TOKENS * tok_cos
            top = ", ".join(profile.top_repos(2)) or "no org PRs"
            detail = f"direct={direct:.2f} lang={lang_cos:.2f} tokens={tok_cos:.2f} top:[{top}]"
            return m.id, (fit, detail)

        results = await asyncio.gather(*(_one(m) for m in candidates), return_exceptions=True)

    out: Dict[str, Tuple[float, str]] = {}
    for res in results:
        if isinstance(res, BaseException):
            continue
        mid, scored = res
        if scored is not None:
            out[mid] = scored
    return out or None


def _ranked_choice(
    qas: List[TeamMember], cfg: TeamAssignmentsConfig, fits: Dict[str, Tuple[float, str]]
) -> Tuple[Optional[TeamMember], str]:
    """Rank by (base + fit) * weight * hardware bias * jitter; return winner + ranking text."""
    jitter = cfg.random_jitter
    rows: List[Tuple[float, TeamMember, str]] = []
    for m in qas:
        fit, detail = fits.get(m.id, (0.0, "no GitHub profile"))
        score = (FIT_BASE_SCORE + fit) * max(0.05, m.weight) * _tier_bias(cfg, m.tier)
        if jitter > 0:
            score *= 1.0 + random.uniform(-jitter, jitter)
        rows.append((score, m, detail))
    rows.sort(key=lambda r: (-r[0], -(r[1].weight), r[1].display))
    if not rows:
        return None, ""
    ranking = " > ".join(f"{m.display}:{s:.3f}" for s, m, _ in rows[:4])
    _, winner, detail = rows[0]
    return winner, f"fit[{detail}] ranking[{ranking}]"


async def pick_qa_for_repo(full_name: str, cfg: Optional[TeamAssignmentsConfig] = None) -> Tuple[Optional[str], str]:
    """
    Returns (plaky_person_id_or_value, reason_summary).
    Uses tier from repos.yml, or auto-classifies if not found.
    """
    cfg = cfg or load_team_assignments()
    fn = (full_name or "").strip()
    if not fn:
        return None, "empty repo"

    if not cfg.members:
        return (
            None,
            "no team members loaded (GitHub roster failed, use_github_support_team_roster=false with no static "
            "members list, or every roster login lacks a Plaky id — set member_overrides[login].id or enable "
            "auto_match_plaky_ids)",
        )

    repo_tier = 2
    routing = get_routing(fn, "", settings.github_org)
    if routing and routing.tier > 0:
        repo_tier = routing.tier
    else:
        repo_tier = await _auto_classify_repo_tier(fn)

    with_qa_role = [m for m in cfg.members if "qa" in m.roles]
    if not with_qa_role:
        return (
            None,
            "no team members have role 'qa' (set member_defaults.roles to include 'qa', "
            "or add qa under member_overrides for each GitHub login; Plaky id required per roster member)",
        )

    qas = [m for m in with_qa_role if repo_matches_member(fn, m)]
    if not qas:
        return (
            None,
            f"no QA-role member matches repo {fn!r} (check repo_globs / explicit_repos); "
            f"{len(with_qa_role)} member(s) have qa role",
        )

    # Filter by QA tier (repo_tier is the tier required - QAs must have qa_tier >= repo_tier)
    tier_before = list(qas)
    qas = [m for m in qas if m.qa_tier >= repo_tier]
    if not qas:
        return (
            None,
            f"no QA after tier filter: repo requires qa_tier>={repo_tier}; "
            f"candidates had qa_tiers {[m.qa_tier for m in tier_before]}",
        )

    # Pattern rules from team_assignments.yml qa_repo_rules (tier 1 allowlist / tier 2 exclusions).
    rules_before = list(qas)
    qas = [m for m in qas if qa_tier_allows_repo(m.qa_tier, fn, cfg.qa_repo_rules)]
    if not qas:
        return (
            None,
            f"no QA after qa_repo_rules filter for {fn!r} "
            f"({len(rules_before)} candidate(s) passed the numeric tier filter)",
        )

    if repo_is_heavy(fn, cfg.heavy_repo_patterns):
        qas = [m for m in qas if m.tier.lower() not in ("light", "minimal", "low")]
        if not qas:
            return None, "heavy repo: no QA after legacy hardware tier filter (light/minimal/low dropped)"

    # GitHub-fit scored ranking; legacy overlap-pool weighted-random as the fallback.
    fits: Optional[Dict[str, Tuple[float, str]]] = None
    try:
        fits = await asyncio.wait_for(_github_fit_scores(qas, fn), timeout=FIT_SCORING_TIMEOUT_SECONDS)
    except Exception as e:  # noqa: BLE001 — never block assignment on scoring (incl. timeout)
        _log.warning("qa_picker: GitHub fit scoring unavailable for %s: %s", fn, e)

    if fits:
        chosen, rank_detail = _ranked_choice(qas, cfg, fits)
        if chosen:
            return (
                chosen.id,
                f"qa={chosen.display} repo_tier={repo_tier} candidates={len(qas)} {rank_detail}",
            )

    pool = _overlap_component(qas)
    chosen = _weighted_choice(pool, cfg)
    if not chosen:
        return None, "weighted pick failed"
    return (
        chosen.id,
        f"qa={chosen.display} pool_size={len(pool)} repo_tier={repo_tier} (legacy weighted pick; GitHub fit unavailable)",
    )


def github_repo_suffix_name(full: str) -> str:
    """Return repository name only (segment after last ``/``); unchanged if no slash."""
    s = (full or "").strip()
    if not s:
        return ""
    return s.rsplit("/", 1)[-1] if "/" in s else s


def ensure_github_owner_repo(slug: str) -> str:
    """If ``slug`` has no ``owner/`` prefix, prepend bare-repo owner (see settings.github_bare_repo_owner)."""
    s = (slug or "").strip()
    if not s or "/" in s:
        return s
    org = (settings.github_bare_repo_owner or "").strip() or (settings.github_org or "").strip()
    if org:
        return f"{org}/{s}"
    return s


def _tokenize_repo_slugs(text: str) -> List[str]:
    """Split comma/newline/whitespace-separated repo tokens (CLI ``--github-repo a b`` / agent ``repo_tag``)."""
    out: List[str] = []
    for chunk in (text or "").replace("\n", ",").split(","):
        for p in chunk.replace("\t", " ").split():
            if p.strip():
                out.append(p.strip())
    return out


def _dedupe_repo_list(repos: Optional[List[str]]) -> List[str]:
    out: List[str] = []
    seen_set: set[str] = set()
    if not repos:
        return out
    for raw in repos:
        s = ensure_github_owner_repo(str(raw or "").strip())
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
        tokens.extend(_tokenize_repo_slugs(primary_repo))
    if isinstance(github_repos, list):
        for repo in github_repos:
            tokens.extend(_tokenize_repo_slugs(str(repo or "")))
    raw_extra = (extra_repo_text or "").strip()
    if raw_extra:
        tokens.extend(_tokenize_repo_slugs(raw_extra))
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
    plaky_field_qa_key: Optional[str] = None,
    repo_value_format: str = "full",
    github_repos_value_format: str = "full",
) -> Dict[str, str]:
    """Map Plaky field key -> QA person id or repo label(s) for create/patch. Overrides win for same keys."""
    cfg = cfg or load_team_assignments()
    out: Dict[str, str] = {}
    qa_key = (plaky_field_qa_key or cfg.plaky_field_qa or "").strip()
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
