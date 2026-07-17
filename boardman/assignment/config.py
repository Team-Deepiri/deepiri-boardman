"""Load team_assignments.yml (QA roster, member roles, Plaky field keys)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
import shutil
import time
from typing import Any, Dict, List, Optional

import yaml

from boardman.assignment.identity_match import best_plaky_match_for_github
from boardman.assignment.llm_identity_match import clear_identity_llm_cache
from boardman.assignment.repo_rules import QaRepoRules, default_qa_repo_rules

# QA leads/managers who must NEVER be auto-assigned to review PRs (employer requirement).
# Matching is case-insensitive against member display name AND GitHub login.
# Override the whole list with `qa_excluded:` in team_assignments.yml.
DEFAULT_QA_EXCLUDED: tuple[str, ...] = (
    "Joe Black",
    "Austin Heitzman",
    "Devin Gamble",
    "Sean San",
    "Nathan Adams",
)
from boardman.github.team_roster import clear_support_team_cache, get_cached_support_team_roster
from boardman.plaky.board_schema import (
    field_likely_github_repo_column,
    field_likely_person_column,
    field_row_item_key,
    plaky_field_row_label,
)
from boardman.plaky.client import PlakyClient
from boardman.settings import settings

_log = logging.getLogger(__name__)
_last_field_sync_by_board: Dict[str, float] = {}


@dataclass
class TierSpec:
    name: str
    weight_bias: float = 1.0


@dataclass
class TeamMember:
    id: str
    display: str = ""
    github_login: str = ""  # GitHub login from org team roster (cross-reference)
    roles: List[str] = field(default_factory=list)
    tier: str = "standard"  # light|standard|heavy — hardware weight bias, not QA repo tier
    qa_tier: int = 3  # 1 = web/core only, 2 = all except AI/heavy repos, 3 = all repos
    repo_globs: List[str] = field(default_factory=list)
    explicit_repos: List[str] = field(default_factory=list)
    weight: float = 1.0


@dataclass
class AmbiguousPRConfig:
    enabled: bool = False
    triage_board_id: str = ""
    triage_group_id: str = ""
    assign_qa: bool = True
    title_template: str = "Triage: PR #{number} — {repo}"


@dataclass
class TeamAssignmentsConfig:
    plaky_field_engineer: str = ""
    plaky_field_qa: str = ""
    plaky_field_repo: str = ""
    plaky_field_github_repos: str = "" # for use if more than one repo connected to the task
    tiers: Dict[str, TierSpec] = field(default_factory=dict)
    members: List[TeamMember] = field(default_factory=list)
    heavy_repo_patterns: List[str] = field(default_factory=list)
    qa_repo_rules: QaRepoRules = field(default_factory=default_qa_repo_rules)
    random_jitter: float = 0.12
    ambiguous_pr: AmbiguousPRConfig = field(default_factory=AmbiguousPRConfig)
    qa_excluded: List[str] = field(default_factory=lambda: list(DEFAULT_QA_EXCLUDED))


def _path() -> Path:
    p = Path(settings.team_assignments_yml_path)
    if p.is_absolute():
        return p
    return Path.cwd() / p


def _example_path() -> Path:
    path = _path()
    return path.with_name(f"{path.name}.example")


@lru_cache
def _raw() -> Dict[str, Any]:
    path = _path()
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def reload_team_assignments() -> None:
    _raw.cache_clear()
    clear_support_team_cache()
    clear_identity_llm_cache()


def ensure_team_assignments_file_exists() -> Path:
    """Create team_assignments.yml from the example file when missing."""
    path = _path()
    if path.is_file():
        return path
    if path.exists() and not path.is_file():
        _log.warning("team_assignments path exists but is not a file: %s", path)
        return path
    ex = _example_path()
    if ex.is_file():
        shutil.copyfile(ex, path)
    else:
        path.write_text("plaky_field_keys:\n  engineer: \"\"\n  qa: \"\"\n  repo: \"\"\n  github_repos: \"\"\n", encoding="utf-8")
    reload_team_assignments()
    return path


def infer_plaky_field_keys_from_normalized(normalized: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """Infer likely team_assignments `plaky_field_keys` entries from board schema field names/types."""
    fields = normalized.get("fields") if isinstance(normalized, dict) else None
    if not isinstance(fields, list):
        return {}

    person_fields: List[tuple[str, str]] = []
    repo_like_fields: List[tuple[str, str]] = []
    for raw in fields:
        if not isinstance(raw, dict):
            continue
        key = field_row_item_key(raw)
        name = plaky_field_row_label(raw)
        if not key:
            continue
        if field_likely_person_column(raw):
            person_fields.append((key, name))
            continue
        if field_likely_github_repo_column(raw):
            repo_like_fields.append((key, name))

    out: Dict[str, str] = {}

    for key, name in person_fields:
        if "qa" in name or "quality" in name:
            out.setdefault("qa", key)
            continue
        if any(tok in name for tok in ("engineer", "developer", "dev", "contributor", "owner", "assignee")):
            out.setdefault("engineer", key)

    if "engineer" not in out:
        for key, _ in person_fields:
            if key != out.get("qa"):
                out["engineer"] = key
                break
    if "qa" not in out and person_fields:
        out["qa"] = person_fields[0][0]

    singular_repo_tokens = ("github repo", "repository", "repo")
    plural_repo_tokens = ("github repos", "repositories", "repo list", "repos")
    for key, name in repo_like_fields:
        if any(tok in name for tok in plural_repo_tokens):
            out.setdefault("github_repos", key)
    for key, name in repo_like_fields:
        if key == out.get("github_repos"):
            continue
        if any(tok in name for tok in singular_repo_tokens):
            out.setdefault("repo", key)
    if "repo" not in out and repo_like_fields:
        out["repo"] = repo_like_fields[0][0]
    if "github_repos" not in out and len(repo_like_fields) >= 2:
        out["github_repos"] = next((k for k, _ in repo_like_fields if k != out.get("repo")), repo_like_fields[1][0])
    return out


async def sync_team_assignment_field_keys_from_board(board_id: str) -> Dict[str, Any]:
    """
    Fill blank `plaky_field_keys` entries in team_assignments.yml from the default board schema.
    Existing non-empty values are preserved.
    """
    bid = (board_id or "").strip()
    if not bid:
        return {"ok": False, "skipped": True, "message": "board_id is empty"}
    cooldown = max(0.0, float(settings.plaky_team_assignment_field_sync_cooldown_seconds or 0.0))
    now = time.monotonic()
    if cooldown > 0:
        last = _last_field_sync_by_board.get(bid)
        if last is not None and (now - last) < cooldown:
            wait_for = round(cooldown - (now - last), 2)
            return {
                "ok": True,
                "skipped": True,
                "message": f"Sync cooldown active for board {bid}; try again in {wait_for}s",
                "board_id": bid,
            }

    path = ensure_team_assignments_file_exists()
    if not path.is_file():
        return {"ok": False, "skipped": True, "message": f"team_assignments path is not a file: {path}"}

    from boardman.plaky.board_schema import fetch_board_schema_bundle

    bundle = await fetch_board_schema_bundle(bid)
    normalized = bundle.get("normalized") if isinstance(bundle, dict) else None
    inferred = infer_plaky_field_keys_from_normalized(normalized)
    if not inferred:
        _last_field_sync_by_board[bid] = now
        return {"ok": False, "skipped": True, "message": "No field keys inferred from board schema", "bundle": bundle}

    current = _raw()
    data: Dict[str, Any] = dict(current) if isinstance(current, dict) else {}
    keys = data.get("plaky_field_keys")
    if not isinstance(keys, dict):
        keys = {}
        data["plaky_field_keys"] = keys

    updated: Dict[str, str] = {}
    for name, inferred_key in inferred.items():
        cur = str(keys.get(name) or "").strip()
        if cur or not inferred_key:
            continue
        keys[name] = inferred_key
        updated[name] = inferred_key

    if not updated:
        _last_field_sync_by_board[bid] = now
        return {"ok": True, "updated": {}, "skipped": True, "message": "No blank field keys needed updating"}

    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=False)
    _last_field_sync_by_board[bid] = now
    reload_team_assignments()
    return {"ok": True, "updated": updated, "path": str(path)}


def _parse_roles(val: Any) -> List[str]:
    if isinstance(val, str) and val.strip():
        return [val.strip().lower()]
    if isinstance(val, list):
        return [str(r).lower() for r in val if r]
    return []


def _parse_glob_list(val: Any) -> List[str]:
    if isinstance(val, str) and val.strip():
        return [val.strip()]
    if isinstance(val, list):
        return [str(g).strip() for g in val if str(g).strip()]
    return []


def _parse_explicit_repos(val: Any) -> List[str]:
    if isinstance(val, str) and val.strip():
        return [val.strip().lower()]
    if isinstance(val, list):
        return [str(r).strip().lower() for r in val if str(r).strip()]
    return []


def _augment_repo_globs_with_github_org(globs: List[str]) -> List[str]:
    """Append owner/* globs from github_bare_repo_owner and github_org when missing (roster matching)."""
    seen = {g.strip().lower() for g in globs if g and str(g).strip()}
    out: List[str] = list(globs)
    for raw in (
        (settings.github_bare_repo_owner or "").strip(),
        (settings.github_org or "").strip(),
    ):
        if not raw:
            continue
        extra = f"{raw.lower()}/*"
        if extra not in seen:
            seen.add(extra)
            out.append(extra)
    return out


def _parse_qa_tier(val: Any) -> int:
    if val is None or val == "":
        return 3
    try:
        t = int(val)
    except (TypeError, ValueError):
        return 3
    return t if t in (1, 2, 3) else 3


def _members_from_github_roster(data: Dict[str, Any]) -> List[TeamMember]:
    """
    Roster = GitHub org team (e.g. Team-Deepiri/support-team). Names/logins from API;
    Plaky field ids and roles come from member_overrides[github_login] (+ member_defaults).
    """
    if data.get("use_github_support_team_roster") is False:
        return []

    spec = str(data.get("github_support_team") or settings.github_support_team).strip()
    ov_raw = data.get("member_overrides") or {}
    if not isinstance(ov_raw, dict):
        ov_raw = {}
    overrides: Dict[str, Dict[str, Any]] = {}
    for k, v in ov_raw.items():
        key = str(k).strip().lower()
        if key and isinstance(v, dict):
            overrides[key] = v

    defaults = data.get("member_defaults") or {}
    if not isinstance(defaults, dict):
        defaults = {}

    roster = get_cached_support_team_roster(spec)
    if not roster.get("ok"):
        _log.warning(
            "Could not load GitHub support team %s for assignments: %s",
            spec,
            roster.get("message"),
        )
        return []

    auto_match = data.get("auto_match_plaky_ids", True) is not False
    min_score = 640
    amb_margin = 45
    if data.get("auto_match_min_score") is not None:
        try:
            min_score = int(data["auto_match_min_score"])
        except (TypeError, ValueError):
            pass
    if data.get("auto_match_ambiguity_margin") is not None:
        try:
            amb_margin = int(data["auto_match_ambiguity_margin"])
        except (TypeError, ValueError):
            pass

    plaky_users: List[Dict[str, Any]] = []
    if auto_match:
        pr = PlakyClient().list_workspace_users_sync()
        if pr.get("ok"):
            plaky_users = [u for u in (pr.get("users") or []) if isinstance(u, dict)]
        else:
            _log.warning(
                "Could not list Plaky users for GitHub↔Plaky auto-match: %s",
                pr.get("message"),
            )

    members: List[TeamMember] = []
    for gh in roster.get("members") or []:
        if not isinstance(gh, dict):
            continue
        login = (gh.get("login") or "").strip()
        if not login:
            continue
        ov = overrides.get(login.lower(), {})
        plaky_id = str(ov.get("id") or ov.get("plaky_id") or "").strip()
        if not plaky_id and auto_match and plaky_users:
            matched, reason, sc = best_plaky_match_for_github(
                gh,
                plaky_users,
                min_score=min_score,
                ambiguity_margin=amb_margin,
            )
            if matched:
                plaky_id = matched
                _log.debug(
                    "auto_match Plaky user %s for GitHub %s (score=%s)",
                    matched,
                    login,
                    sc,
                )
            elif reason == "ambiguous" and sc > 0:
                _log.debug(
                    "auto_match ambiguous for GitHub %s (best score=%s, raise auto_match_ambiguity_margin or set id in member_overrides)",
                    login,
                    sc,
                )
        if not plaky_id:
            continue

        roles_src = ov.get("roles") if "roles" in ov else defaults.get("roles")
        roles = _parse_roles(roles_src)
        if not roles:
            roles = ["engineer"]

        globs_src = ov.get("repo_globs") or ov.get("repos_globs")
        if globs_src is None:
            globs_src = defaults.get("repo_globs") or defaults.get("repos_globs")
        globs = _parse_glob_list(globs_src)
        globs = _augment_repo_globs_with_github_org(globs)

        ex_src = ov.get("repos") or ov.get("explicit_repos")
        if ex_src is None:
            ex_src = defaults.get("repos") or defaults.get("explicit_repos")
        explicit = _parse_explicit_repos(ex_src)

        qt = _parse_qa_tier(ov.get("qa_tier") if "qa_tier" in ov else defaults.get("qa_tier"))
        tier = str(
            ov.get("tier") if "tier" in ov else defaults.get("tier") or "standard"
        ).lower()
        weight = float(ov.get("weight") if "weight" in ov else defaults.get("weight", 1.0))

        display = str(ov.get("display") or ov.get("name") or gh.get("name") or login)

        members.append(
            TeamMember(
                id=plaky_id,
                display=display,
                github_login=login,
                roles=roles,
                tier=tier,
                qa_tier=qt,
                repo_globs=globs,
                explicit_repos=explicit,
                weight=weight,
            )
        )

    return members


def load_team_assignments() -> TeamAssignmentsConfig:
    data = _raw()
    keys = data.get("plaky_field_keys") or {}
    if not isinstance(keys, dict):
        keys = {}

    tiers_out: Dict[str, TierSpec] = {}
    tiers_block = data.get("hardware_tiers") or data.get("tiers") or {}
    if isinstance(tiers_block, dict):
        for name, spec in tiers_block.items():
            if isinstance(spec, dict):
                tiers_out[str(name)] = TierSpec(
                    name=str(name),
                    weight_bias=float(spec.get("weight_bias", spec.get("bias", 1.0))),
                )
            else:
                tiers_out[str(name)] = TierSpec(name=str(name), weight_bias=1.0)

    raw_members = data.get("members")
    has_explicit_members = isinstance(raw_members, list) and any(
        isinstance(x, dict) for x in raw_members
    )

    members: List[TeamMember] = []
    if has_explicit_members:
        for m in raw_members or []:
            if not isinstance(m, dict):
                continue
            mid = str(m.get("id") or m.get("plaky_id") or "").strip()
            if not mid:
                continue
            roles = m.get("roles") or []
            if isinstance(roles, str):
                roles = [roles]
            globs = m.get("repo_globs") or m.get("repos_globs") or []
            if isinstance(globs, str):
                globs = [globs]
            explicit = m.get("repos") or m.get("explicit_repos") or []
            if isinstance(explicit, str):
                explicit = [explicit]
            qa_tier = _parse_qa_tier(m.get("qa_tier"))

            gh_login = str(m.get("github_login") or m.get("github") or "").strip()

            members.append(
                TeamMember(
                    id=mid,
                    display=str(m.get("display") or m.get("name") or mid),
                    github_login=gh_login,
                    roles=[str(r).lower() for r in roles if r],
                    tier=str(m.get("tier") or "standard").lower(),
                    qa_tier=qa_tier,
                    repo_globs=[str(g).strip() for g in globs if str(g).strip()],
                    explicit_repos=[str(r).strip().lower() for r in explicit if str(r).strip()],
                    weight=float(m.get("weight", 1.0)),
                )
            )
    elif data.get("use_github_support_team_roster", True) is not False:
        members = _members_from_github_roster(data)

    req = data.get("repo_requirements") or {}
    heavy: List[str] = []
    if isinstance(req, dict):
        hp = req.get("heavy_repo_patterns") or []
        if isinstance(hp, list):
            heavy = [str(x) for x in hp]

    sel = data.get("selection") or {}
    jitter = 0.12
    if isinstance(sel, dict):
        jitter = float(sel.get("random_jitter", sel.get("jitter", 0.12)))

    amb = data.get("ambiguous_pr") or {}
    ambiguous = AmbiguousPRConfig()
    if isinstance(amb, dict):
        ambiguous = AmbiguousPRConfig(
            enabled=bool(amb.get("enabled", False)),
            triage_board_id=str(amb.get("triage_board_id") or ""),
            triage_group_id=str(amb.get("triage_group_id") or ""),
            assign_qa=bool(amb.get("assign_qa", True)),
            title_template=str(amb.get("title_template") or ambiguous.title_template),
        )

    rules = default_qa_repo_rules()
    qr = data.get("qa_repo_rules") or {}
    if isinstance(qr, dict):
        t2 = qr.get("tier2_excluded_patterns") or qr.get("tier2_excluded")
        t1 = qr.get("tier1_only_patterns") or qr.get("tier1_only")
        if isinstance(t2, list) and t2:
            rules.tier2_excluded_patterns = [str(x) for x in t2]
        if isinstance(t1, list) and t1:
            rules.tier1_only_patterns = [str(x) for x in t1]

    excluded = list(DEFAULT_QA_EXCLUDED)
    exc_raw = data.get("qa_excluded")
    if isinstance(exc_raw, list):
        excluded = [str(x).strip() for x in exc_raw if str(x).strip()]

    return TeamAssignmentsConfig(
        plaky_field_engineer=str(keys.get("engineer") or keys.get("assignee_dev") or ""),
        plaky_field_qa=str(keys.get("qa") or keys.get("qa_engineer") or ""),
        plaky_field_repo=str(keys.get("repo") or keys.get("repository") or keys.get("github_repo") or ""),
        plaky_field_github_repos=str(
            keys.get("github_repos") or keys.get("repos") or keys.get("repositories") or ""
        ),
        tiers=tiers_out,
        members=members,
        heavy_repo_patterns=heavy,
        qa_repo_rules=rules,
        random_jitter=max(0.0, min(jitter, 0.5)),
        ambiguous_pr=ambiguous,
        qa_excluded=excluded,
    )
