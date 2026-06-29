"""
PR ↔ Plaky task linking: candidate generation, deterministic scoring, optional LLM rerank.

Stages:
  A) Recall-focused candidate generation (DB mappings + board items).
  B) Precision-focused composite score + negative signals.
  C) Optional LLM rerank among top-K when score is in the medium band.

Designed for pull_request.opened when no Fixes/Closes issue keywords are present.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from boardman.assignment.identity_match import score_github_vs_plaky
from boardman.database.models import IssueTaskMap, SyncLog
from boardman.plaky.board_schema import fetch_board_schema_bundle
from boardman.plaky.client import PlakyClient
from boardman.repos_config import get_routing
from boardman.services.issue_handler import get_linked_issue_numbers
from boardman.services.llm_pr_task_rerank import llm_rerank_pr_candidates
from boardman.settings import settings

# --- extraction -----------------------------------------------------------------

GITHUB_ISSUE_URL_RE = re.compile(
    r"https?://github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)/(?:issues|pull)/(?P<num>\d+)",
    re.I,
)
BODY_HASH_ISSUE_RE = re.compile(r"(?<!\w)#(\d+)\b")
BRANCH_ISSUE_NUM_RE = re.compile(r"(?:^|[-_/])(\d{1,6})(?=[-_/.]|$)")


def github_head_ref(head: Any) -> str:
    if isinstance(head, dict):
        return str(head.get("ref") or "").strip()
    return ""


def referenced_issue_numbers(
    *,
    repo_full: str,
    pr_title: str,
    pr_body: str | None,
    head_ref: str,
) -> set[int]:
    """Issue numbers the PR plausibly refers to (branch, URLs for this repo, #mentions)."""
    out: set[int] = set()
    text = f"{pr_title}\n{pr_body or ''}"
    owner, repo = "", ""
    if "/" in repo_full:
        owner, repo = repo_full.split("/", 1)

    for m in GITHUB_ISSUE_URL_RE.finditer(text):
        o, r, n = m.group("owner"), m.group("repo"), m.group("num")
        if (o, r.lower()) == (owner, repo.lower()) or (f"{o}/{r}".lower() == repo_full.lower()):
            out.add(int(n))

    for m in BODY_HASH_ISSUE_RE.finditer(text):
        out.add(int(m.group(1)))

    ref = head_ref.replace("refs/heads/", "")
    for part in ref.split("/"):
        for m in BRANCH_ISSUE_NUM_RE.finditer(part):
            n = int(m.group(1))
            if 1 <= n < 1_000_000:
                out.add(n)

    return out


def _similar(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.casefold(), b.casefold()).ratio()


def _cosine_word_similarity(a: str, b: str) -> float:
    """Cosine similarity on word-frequency vectors (lightweight semantic-ish signal)."""

    def _tok(s: str) -> list[str]:
        return re.findall(r"[a-z0-9]+", s.casefold())

    ca, cb = Counter(_tok(a)), Counter(_tok(b))
    if not ca or not cb:
        return 0.0
    dot = sum(ca[t] * cb.get(t, 0) for t in ca)
    na = math.sqrt(sum(v * v for v in ca.values()))
    nb = math.sqrt(sum(v * v for v in cb.values()))
    if not na or not nb:
        return 0.0
    return dot / (na * nb)


def _blend_seq_cos(seq: float, cos: float, weight: float) -> float:
    w = max(0.0, min(1.0, weight))
    return (1.0 - w) * seq + w * cos


def _norm_item_id(item: dict[str, Any]) -> str:
    return str(item.get("id") or item.get("itemId") or item.get("_id") or "").strip()


def _item_title_desc(item: dict[str, Any]) -> tuple[str, str]:
    title = str(item.get("name") or item.get("title") or "").strip()
    desc = str(
        item.get("description") or item.get("body") or item.get("content") or item.get("text") or ""
    ).strip()
    return title, desc


def issue_numbers_in_text(text: str, repo_full: str) -> set[int]:
    nums: set[int] = set()
    rf = repo_full.lower()
    for m in GITHUB_ISSUE_URL_RE.finditer(text or ""):
        if f"{m.group('owner')}/{m.group('repo')}".lower() == rf:
            nums.add(int(m.group("num")))
    for m in BODY_HASH_ISSUE_RE.finditer(text or ""):
        nums.add(int(m.group(1)))
    return nums


# --- candidates -----------------------------------------------------------------


@dataclass
class TaskCandidate:
    task_id: str
    title: str
    description: str
    status: str | None = None
    issue_numbers: set[int] = field(default_factory=set)
    sources: list[str] = field(default_factory=list)
    assignee_login: str | None = None
    assignee_email: str | None = None
    assignee_name: str | None = None


@dataclass
class ScoredCandidate:
    task_id: str
    title: str
    description: str
    score: float
    breakdown: dict[str, float]
    status: str | None = None


Decision = Literal["none", "auto_link", "llm_link", "triage"]


@dataclass
class PipelineResult:
    decision: Decision
    task_id: str | None
    score: float
    reason: str
    top_scored: list[ScoredCandidate] = field(default_factory=list)
    log_detail: dict[str, Any] = field(default_factory=dict)


async def _db_mappings_for_repo(session: AsyncSession, repo_name: str) -> list[IssueTaskMap]:
    r = await session.execute(select(IssueTaskMap).where(IssueTaskMap.github_repo == repo_name))
    return list(r.scalars().all())


async def _other_pr_linked_to_task(
    session: AsyncSession,
    plaky_task_id: str,
    current_pr_number: int,
    github_repo: str,
    limit: int = 40,
) -> bool:
    from boardman.services.pr_task_registry import has_other_open_pr_for_task

    if await has_other_open_pr_for_task(
        session,
        plaky_task_id=plaky_task_id,
        github_repo=github_repo,
        current_pr_number=current_pr_number,
    ):
        return True

    q = (
        select(SyncLog)
        .where(
            SyncLog.action.in_(("pr_linked", "pr_linked_fuzzy")),
            SyncLog.plaky_task_id == plaky_task_id,
        )
        .order_by(SyncLog.created_at.desc())
        .limit(limit)
    )
    r = await session.execute(q)
    for row in r.scalars():
        ref = (row.github_ref or "").strip()
        if ref.isdigit() and int(ref) != current_pr_number:
            gr = (row.github_repo or "").strip()
            if gr and gr != github_repo:
                continue
            return True
    return False


def _merge_candidate(
    by_id: dict[str, TaskCandidate],
    task_id: str,
    title: str,
    description: str,
    issue_nums: set[int],
    source: str,
    assignee_login: str | None = None,
    assignee_email: str | None = None,
    assignee_name: str | None = None,
    status: str | None = None,
) -> None:
    if not task_id:
        return
    if task_id not in by_id:
        by_id[task_id] = TaskCandidate(
            task_id=task_id,
            title=title,
            description=description,
            status=status,
            issue_numbers=set(issue_nums),
            sources=[source],
            assignee_login=assignee_login,
            assignee_email=assignee_email,
            assignee_name=assignee_name,
        )
        return
    c = by_id[task_id]
    c.sources.append(source)
    c.issue_numbers |= issue_nums
    if len(title) > len(c.title):
        c.title = title
    if len(description) > len(c.description):
        c.description = description

    # Update status if current is empty or if we found a more "active" looking status
    if status and (not c.status or c.status.lower() in ("done", "closed", "completed")):
        c.status = status

    # Update assignee info if provided and not already set
    if assignee_login and not c.assignee_login:
        c.assignee_login = assignee_login
    if assignee_email and not c.assignee_email:
        c.assignee_email = assignee_email
    if assignee_name and not c.assignee_name:
        c.assignee_name = assignee_name


async def gather_candidates(
    *,
    session: AsyncSession,
    repo_name: str,
    repo_full: str,
    board_id: str,
    plaky: PlakyClient,
) -> dict[str, TaskCandidate]:
    by_id: dict[str, TaskCandidate] = {}

    for m in await _db_mappings_for_repo(session, repo_name):
        nums = {m.github_issue_number}
        _merge_candidate(
            by_id,
            m.plaky_task_id,
            f"[{repo_name}] mapped issue #{m.github_issue_number}",
            "",
            nums,
            "issue_task_map",
        )

    if not board_id or not settings.pr_linking_fetch_board_items:
        return by_id

    listed = await plaky.list_board_items(board_id, max_pages=settings.pr_linking_board_max_pages)
    if not listed.get("ok"):
        return by_id

    # Fetch workspace users for assignee mapping
    users_result = await plaky.list_workspace_users()
    workspace_users = users_result.get("users", []) if users_result.get("ok") else []

    # Build assignee lookup: user_id -> {login, email, name}
    user_lookup: dict[str, dict] = {}
    for u in workspace_users:
        uid = str(u.get("id") or "")
        if uid:
            user_lookup[uid] = {
                "login": "",  # Plaky doesn't have GitHub login, use name as fallback
                "email": str(u.get("primaryEmail") or u.get("email") or "").lower(),
                "name": str(u.get("name") or u.get("displayName") or ""),
            }

    # Hardcoded assignee field keys
    assignee_field_keys = ["engineer", "qa", "assignee_dev", "assignee_qa", "person"]
    status_field_keys = ["status", "state", "stage", "progress"]

    max_items = max(1, settings.pr_linking_max_board_items_scan)
    tag = f"[{repo_name}]"
    rf_low = repo_full.lower()
    owner = repo_full.split("/")[0] if "/" in repo_full else ""

    for item in (listed.get("items") or [])[:max_items]:
        if not isinstance(item, dict):
            continue
        tid = _norm_item_id(item)
        title, desc = _item_title_desc(item)
        if not tid:
            continue

        # Extract assignee info from item fields
        assignee_login = None
        assignee_email = None
        assignee_name = None
        status = str(item.get("status") or "").strip() or None

        raw_fields = item.get("fields")
        if isinstance(raw_fields, dict):
            # Status check in fields
            if not status:
                for sk in status_field_keys:
                    for fk, fv in raw_fields.items():
                        if sk in fk.lower() and isinstance(fv, str):
                            status = fv
                            break
                    if status:
                        break

            # Assignee check in fields
            for field_key in assignee_field_keys:
                assignee_id = raw_fields.get(field_key)
                if assignee_id and str(assignee_id) in user_lookup:
                    assignee_info = user_lookup[str(assignee_id)]
                    assignee_login = assignee_info.get("login") or ""
                    assignee_email = assignee_info.get("email") or ""
                    assignee_name = assignee_info.get("name") or ""
                    break  # Use first matching assignee field

        combined = f"{title}\n{desc}"
        cl = combined.lower()
        nums = issue_numbers_in_text(combined, repo_full)
        mention_repo = (
            tag.lower() in title.lower()
            or tag.lower() in desc.lower()
            or rf_low in cl
            or (owner and f"{owner}/{repo_name}".lower() in cl)
        )
        if mention_repo or nums:
            _merge_candidate(
                by_id,
                tid,
                title,
                desc,
                nums,
                "board_item",
                assignee_login=assignee_login,
                assignee_email=assignee_email,
                assignee_name=assignee_name,
                status=status,
            )

    return by_id


def _tokenize_ref(text: str) -> set[str]:
    """Split branch/ref names into keywords, filtering common prefixes."""
    if not text:
        return set()
    # Replace separators with spaces, lowercase, and split
    clean = re.sub(r"[/_-]", " ", text.lower())
    tokens = {t.strip() for t in clean.split() if len(t.strip()) >= 3}
    # Filter out very common git flow or type prefixes
    ignore = {
        "feat",
        "feature",
        "fix",
        "bug",
        "hotfix",
        "patch",
        "refactor",
        "chore",
        "docs",
        "test",
        "issue",
        "task",
    }
    return tokens - ignore


def score_candidate(
    cand: TaskCandidate,
    *,
    ref_issues: set[int],
    pr_title: str,
    pr_body: str,
    repo_full: str,
    pr_number: int,
    session_penalty: bool,
    head_ref: str = "",
    pr_author_login: str | None = None,
    pr_author_email: str | None = None,
    pr_author_name: str | None = None,
    done_statuses: set[str] | None = None,
    active_statuses: set[str] | None = None,
    cosine_blend: float = 0.0,
) -> ScoredCandidate:
    """Composite score (roughly 0–100+ before clamp)."""
    b: dict[str, float] = {}
    score = 0.0

    overlap = cand.issue_numbers & ref_issues
    if overlap:
        b["issue_ref_overlap"] = 100.0
        score += 100.0
    elif ref_issues and cand.issue_numbers:
        b["issue_ref_mismatch"] = -55.0
        score -= 55.0

    comb = f"{cand.title}\n{cand.description}"
    if pr_body and GITHUB_ISSUE_URL_RE.search(pr_body):
        for m in GITHUB_ISSUE_URL_RE.finditer(pr_body):
            url = m.group(0)
            if url in comb.replace(" ", ""):
                b["url_in_task_text"] = 25.0
                score += 25.0
                break

    # --- Title similarity (SequenceMatcher + optional word-bag cosine) ---
    ts_seq = _similar(pr_title, cand.title)
    ts_cos = _cosine_word_similarity(pr_title, cand.title)
    ts = _blend_seq_cos(ts_seq, ts_cos, cosine_blend)
    b["title_cos"] = round(ts_cos, 4)
    if ts_seq >= 0.98:
        # Near exact match gets a huge boost to clear auto_link threshold
        b["title_exact_match"] = 80.0
        score += 80.0
    else:
        b["title_sim"] = round(ts, 4)
        score += 25.0 * ts

    # --- Branch Name Keyword/Token Matching ---
    if head_ref:
        branch_tokens = _tokenize_ref(head_ref)
        title_tokens = _tokenize_ref(cand.title)

        token_overlap = branch_tokens & title_tokens
        if token_overlap:
            # Score based on how many keywords matched (logarithmic-ish)
            count = len(token_overlap)
            boost = min(30.0, 10.0 + (count * 5.0))
            b["branch_token_match"] = boost
            score += boost

        # Also check fuzzy similarity of full branch name vs title (lower weight)
        bs = _similar(head_ref.split("/")[-1], cand.title)
        if bs >= 0.6:
            b["branch_fuzzy_sim"] = round(bs * 15.0, 2)
            score += bs * 15.0

    body_snip = (pr_body or "")[:8000]
    desc_snip = cand.description[:8000]
    ds_seq = _similar(body_snip, desc_snip)
    ds_cos = _cosine_word_similarity(body_snip, desc_snip)
    ds = _blend_seq_cos(ds_seq, ds_cos, cosine_blend)
    b["body_cos"] = round(ds_cos, 4)
    b["body_sim"] = round(ds, 4)
    score += 10.0 * ds

    if session_penalty:
        b["other_pr_linked"] = -40.0
        score -= 40.0

    # --- Status weighting ---
    if cand.status:
        st = cand.status
        # Use schema-derived sets if provided
        if done_statuses and st in done_statuses:
            b["status_closed_penalty"] = -30.0
            score -= 30.0
        elif active_statuses and st in active_statuses:
            b["status_active_boost"] = 15.0
            score += 15.0
        else:
            # Fallback to keyword matching if schema is empty or doesn't match
            st_low = st.lower()
            if any(
                x in st_low
                for x in (
                    "done",
                    "closed",
                    "completed",
                    "resolved",
                    "finished",
                    "archive",
                    "shipped",
                    "live",
                    "merged",
                )
            ):
                b["status_closed_penalty"] = -30.0
                score -= 30.0
            elif any(
                x in st_low
                for x in (
                    "progress",
                    "doing",
                    "active",
                    "todo",
                    "to do",
                    "backlog",
                    "dev",
                    "qa",
                    "review",
                )
            ):
                b["status_active_boost"] = 15.0
                score += 15.0

    # --- Person matching: PR author vs task assignee (via identity_match) ---
    if pr_author_login or pr_author_email or pr_author_name:
        pr_author: dict[str, Any] = {
            "login": pr_author_login,
            "email": pr_author_email,
            "name": pr_author_name,
        }
        plaky_user: dict[str, Any] = {
            "login": cand.assignee_login,
            "email": cand.assignee_email,
            "name": cand.assignee_name,
        }

        if (pr_author.get("login") or pr_author.get("email") or pr_author.get("name")) and (
            plaky_user.get("login") or plaky_user.get("email") or plaky_user.get("name")
        ):
            identity_score = score_github_vs_plaky(pr_author, plaky_user)
            if identity_score >= 6500:
                b["assignee_identity_match"] = 50.0
                score += 50.0
            elif identity_score >= 5000:
                b["assignee_identity_partial"] = 30.0
                score += 30.0
            elif identity_score >= 3500:
                b["assignee_identity_weak"] = 15.0
                score += 15.0

    # --- PR title name boost: if title contains assignee name ---
    if pr_author_name and cand.assignee_name:
        title_lower = pr_title.lower()
        assignee_lower = cand.assignee_name.lower()
        assignee_parts = assignee_lower.split()
        name_boost = 0
        for part in assignee_parts:
            if len(part) >= 3 and part in title_lower:
                name_boost = 20.0
                break
        if name_boost > 0:
            b["pr_title_name_mention"] = name_boost
            score += name_boost

    b["total"] = round(score, 2)
    return ScoredCandidate(
        task_id=cand.task_id,
        title=cand.title,
        description=cand.description,
        score=score,
        breakdown=b,
        status=cand.status,
    )


async def run_pr_task_pipeline(
    *,
    session: AsyncSession,
    plaky: PlakyClient,
    repo_full: str,
    repo_name: str,
    org: str,
    pr_number: int,
    pr_title: str,
    pr_body: str | None,
    head: Any,
    pr_author_login: str | None = None,
    pr_author_email: str | None = None,
    pr_author_name: str | None = None,
) -> PipelineResult:
    """
    Run full pipeline. Call when standard Fixes/Closes links are absent.
    """
    if not settings.pr_linking_pipeline_enabled:
        return PipelineResult(
            decision="none",
            task_id=None,
            score=0.0,
            reason="pipeline_disabled",
            log_detail={"enabled": False},
        )

    head_ref = github_head_ref(head)
    ref_issues = referenced_issue_numbers(
        repo_full=repo_full,
        pr_title=pr_title,
        pr_body=pr_body,
        head_ref=head_ref,
    )

    routing = get_routing(repo_full, repo_name, org)
    board_id = (routing.plaky_board_id if routing and routing.plaky_board_id else "") or ""
    board_id = board_id.strip()

    # Dynamic status detection from schema
    done_set: set[str] = set()
    active_set: set[str] = set()
    if board_id:
        schema = await fetch_board_schema_bundle(board_id)
        if schema.get("ok") and schema.get("normalized"):
            fields = schema["normalized"].get("fields") or []
            for f in fields:
                if f.get("type") == "status" or "status" in f.get("name", "").lower():
                    opts = f.get("options") or []
                    for opt in opts:
                        name = opt.get("name") if isinstance(opt, dict) else str(opt)
                        if not name:
                            continue
                        nl = name.lower()
                        if any(
                            x in nl
                            for x in (
                                "done",
                                "closed",
                                "completed",
                                "resolved",
                                "finished",
                                "archive",
                                "shipped",
                                "live",
                                "merged",
                            )
                        ):
                            done_set.add(name)
                        elif any(
                            x in nl
                            for x in (
                                "progress",
                                "doing",
                                "active",
                                "todo",
                                "to do",
                                "backlog",
                                "dev",
                                "qa",
                                "review",
                            )
                        ):
                            active_set.add(name)

    candidates = await gather_candidates(
        session=session,
        repo_name=repo_name,
        repo_full=repo_full,
        board_id=board_id,
        plaky=plaky,
    )

    if not candidates:
        detail = {
            "ref_issues": sorted(ref_issues),
            "head_ref": head_ref,
            "candidate_count": 0,
        }
        return PipelineResult(
            decision="none",
            task_id=None,
            score=0.0,
            reason="no_candidates",
            log_detail=detail,
        )

    scored: list[ScoredCandidate] = []
    for cand in candidates.values():
        conflict = await _other_pr_linked_to_task(
            session, cand.task_id, pr_number, github_repo=repo_name
        )
        scored.append(
            score_candidate(
                cand,
                ref_issues=ref_issues,
                pr_title=pr_title,
                pr_body=pr_body or "",
                repo_full=repo_full,
                pr_number=pr_number,
                session_penalty=conflict,
                head_ref=head_ref,
                pr_author_login=pr_author_login,
                pr_author_email=pr_author_email,
                pr_author_name=pr_author_name,
                done_statuses=done_set,
                active_statuses=active_set,
                cosine_blend=float(settings.pr_linking_cosine_weight),
            )
        )

    scored.sort(key=lambda x: x.score, reverse=True)
    top = scored[: max(5, settings.pr_linking_top_n_for_llm)]

    hi = float(settings.pr_linking_high_threshold)
    med = float(settings.pr_linking_medium_threshold)

    best = scored[0]
    detail: dict[str, Any] = {
        "ref_issues": sorted(ref_issues),
        "head_ref": head_ref,
        "candidate_count": len(candidates),
        "top": [
            {"task_id": s.task_id, "score": s.score, "breakdown": s.breakdown} for s in top[:8]
        ],
    }

    if best.score >= hi:
        return PipelineResult(
            decision="auto_link",
            task_id=best.task_id,
            score=best.score,
            reason="deterministic_high",
            top_scored=top,
            log_detail=detail,
        )

    if best.score < med:
        detail["decision"] = "below_medium"
        return PipelineResult(
            decision="triage",
            task_id=None,
            score=best.score,
            reason="below_medium_threshold",
            top_scored=top,
            log_detail=detail,
        )

    # Medium band: optional LLM rerank among top candidates that clear a floor
    floor = max(med, best.score - 25.0)
    medium_pool = [s for s in top if s.score >= floor][: settings.pr_linking_top_n_for_llm]
    if not medium_pool:
        return PipelineResult(
            decision="triage",
            task_id=None,
            score=best.score,
            reason="empty_medium_pool",
            top_scored=top,
            log_detail=detail,
        )

    if settings.pr_linking_llm_enabled:
        tuples = [(s.task_id, s.title, s.description) for s in medium_pool]
        tid, conf, rreason = llm_rerank_pr_candidates(
            repo_full=repo_full,
            pr_title=pr_title,
            pr_body=pr_body or "",
            candidates=tuples,
        )
        detail["llm"] = {"task_id": tid, "confidence": conf, "reason": rreason}
        min_c = float(settings.pr_linking_llm_min_confidence)
        if tid and conf >= min_c:
            match_score = next((s.score for s in medium_pool if s.task_id == tid), best.score)
            return PipelineResult(
                decision="llm_link",
                task_id=tid,
                score=match_score,
                reason="llm_rerank",
                top_scored=top,
                log_detail=detail,
            )
        detail["llm_reject"] = "low_confidence_or_null"
        return PipelineResult(
            decision="triage",
            task_id=None,
            score=best.score,
            reason="llm_reject",
            top_scored=top,
            log_detail=detail,
        )

    detail["llm"] = "disabled"
    return PipelineResult(
        decision="triage",
        task_id=None,
        score=best.score,
        reason="medium_no_llm",
        top_scored=top,
        log_detail=detail,
    )


def closing_issue_numbers(pr_body: str | None) -> list[int]:
    """Sync wrapper path uses async get_linked_issue_numbers; sync helper for tests."""
    import asyncio

    return asyncio.get_event_loop().run_until_complete(get_linked_issue_numbers(pr_body))


async def should_run_pipeline(pr_body: str | None) -> bool:
    """Run when there are no closing keywords linking an issue."""
    linked = await get_linked_issue_numbers(pr_body)
    return not linked


def format_triage_comment(top: Sequence[ScoredCandidate], limit: int = 3) -> str:
    """Human-readable candidate list for future GitHub PR comments."""
    lines = ["**Possible Plaky tasks** (automation could not auto-link confidently):"]
    for s in top[:limit]:
        lines.append(f"- `{s.task_id}` — score {s.score:.1f} — {s.title[:120]}")
    return "\n".join(lines)
