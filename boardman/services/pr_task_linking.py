"""
PR ↔ Plaky task linking: candidate generation, deterministic scoring, optional LLM rerank.

Stages:
  A) Recall-focused candidate generation (DB mappings + board items).
  B) Precision-focused composite score + negative signals.
  C) Optional LLM rerank among top-K when score is in the medium band.

Designed for pull_request.opened when no Fixes/Closes issue keywords are present.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from boardman.database.models import IssueTaskMap, SyncLog
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
    issue_numbers: set[int] = field(default_factory=set)
    sources: list[str] = field(default_factory=list)


@dataclass
class ScoredCandidate:
    task_id: str
    title: str
    description: str
    score: float
    breakdown: dict[str, float]


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
    limit: int = 30,
) -> bool:
    q = (
        select(SyncLog)
        .where(
            SyncLog.action == "pr_linked",
            SyncLog.plaky_task_id == plaky_task_id,
        )
        .order_by(SyncLog.created_at.desc())
        .limit(limit)
    )
    r = await session.execute(q)
    for row in r.scalars():
        ref = (row.github_ref or "").strip()
        if ref.isdigit() and int(ref) != current_pr_number:
            return True
    return False


def _merge_candidate(
    by_id: dict[str, TaskCandidate],
    task_id: str,
    title: str,
    description: str,
    issue_nums: set[int],
    source: str,
) -> None:
    if not task_id:
        return
    if task_id not in by_id:
        by_id[task_id] = TaskCandidate(
            task_id=task_id,
            title=title,
            description=description,
            issue_numbers=set(issue_nums),
            sources=[source],
        )
        return
    c = by_id[task_id]
    c.sources.append(source)
    c.issue_numbers |= issue_nums
    if len(title) > len(c.title):
        c.title = title
    if len(description) > len(c.description):
        c.description = description


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
            _merge_candidate(by_id, tid, title, desc, nums, "board_item")

    return by_id


def score_candidate(
    cand: TaskCandidate,
    *,
    ref_issues: set[int],
    pr_title: str,
    pr_body: str,
    repo_full: str,
    pr_number: int,
    session_penalty: bool,
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

    ts = _similar(pr_title, cand.title)
    b["title_sim"] = round(ts, 4)
    score += 20.0 * ts

    body_snip = (pr_body or "")[:8000]
    ds = _similar(body_snip, cand.description[:8000])
    b["body_sim"] = round(ds, 4)
    score += 10.0 * ds

    if session_penalty:
        b["other_pr_linked"] = -40.0
        score -= 40.0

    b["total"] = round(score, 2)
    return ScoredCandidate(
        task_id=cand.task_id,
        title=cand.title,
        description=cand.description,
        score=score,
        breakdown=b,
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
    board_id = (routing.plaky_board_id if routing else "") or settings.plaky_default_board_id

    candidates = await gather_candidates(
        session=session,
        repo_name=repo_name,
        repo_full=repo_full,
        board_id=board_id.strip(),
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
        conflict = await _other_pr_linked_to_task(session, cand.task_id, pr_number)
        scored.append(
            score_candidate(
                cand,
                ref_issues=ref_issues,
                pr_title=pr_title,
                pr_body=pr_body or "",
                repo_full=repo_full,
                pr_number=pr_number,
                session_penalty=conflict,
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
