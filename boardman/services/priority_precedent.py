"""BM25 precedent-based priority inference from LIVE Plaky board history.

No invented corpus, no trained model, no LLM call: the "historical decisions" this
retrieves against are simply the items already sitting on the target Plaky board --
each one's title/description paired with whatever priority a human (or an earlier run
of this pipeline) already set for it. A new PR's title/body is matched against that
live board history with BM25 (a decades-old statistical bag-of-words retrieval scorer --
no training step, no embeddings, no downloaded weights) and the retrieved precedents
vote on a priority bucket, weighted by how well they matched and how recently that
precedent was itself decided.

Two confidence-only modifiers are computed from the SAME fetched board data:
  - retrieval agreement (how concentrated the vote is on one bucket vs. split across
    several) -- this already falls out of the consensus math below.
  - board saturation (what fraction of the board's OTHER items are already
    High/Very Important right now) -- included ONLY to widen or narrow the confidence
    band, never to move the winning bucket. A busy/saturated board is not evidence this
    PR itself is urgent.

If the board has too little priority-bearing history, or the retrieved precedents
don't agree, the caller falls back to the plain rule-based `infer_priority_from_text`
(priority_rules.py) instead of trusting a thin/noisy signal.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime

from boardman.plaky.board_schema import fetch_board_schema_bundle, plaky_field_row_label
from boardman.plaky.client import PlakyClient
from boardman.plaky.task_tag_vocab import TASK_PRIORITY_TAGS, canonical_task_priority
from boardman.settings import settings

_log = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Same generic "does this column look like Priority" substring test task_mutations.py
# already uses when WRITING a priority field -- reused here only to READ one back.
_PRIORITY_COLUMN_SUBSTRINGS = ("priority", "prio")


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall((text or "").casefold())


def _item_id(item: dict) -> str:
    return str(item.get("id") or item.get("itemId") or item.get("_id") or "").strip()


def _item_title_desc(item: dict) -> tuple[str, str]:
    title = str(item.get("name") or item.get("title") or "").strip()
    desc = str(
        item.get("description") or item.get("body") or item.get("content") or item.get("text") or ""
    ).strip()
    return title, desc


def _item_group_id(item: dict) -> str:
    for key in ("groupId", "group_id"):
        val = item.get(key)
        if val not in (None, ""):
            return str(val)
    group = item.get("group")
    if isinstance(group, dict):
        return str(group.get("id") or "")
    if group not in (None, ""):
        return str(group)
    return ""


def _blob_from_value(v: object) -> str:
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        return str(v.get("name") or v.get("title") or v.get("label") or "")
    return ""


async def _priority_field_key(board_id: str) -> str:
    """Resolve which item-field key holds Priority on this board (schema-driven, not a
    hardcoded id — boards vary their internal field ids)."""
    schema = await fetch_board_schema_bundle(board_id)
    normalized = schema.get("normalized") if isinstance(schema, dict) else None
    fields = normalized.get("fields") if isinstance(normalized, dict) else None
    if not isinstance(fields, list):
        return ""
    for f in fields:
        if not isinstance(f, dict):
            continue
        name = plaky_field_row_label(f)
        if name and any(s in name for s in _PRIORITY_COLUMN_SUBSTRINGS):
            return str(f.get("key") or "").strip()
    return ""


def _item_priority(item: dict, priority_field_key: str) -> str:
    """Raw priority value off a board item, or "" if none is set."""
    raw = _blob_from_value(item.get("priority"))
    if raw:
        return raw
    fields = item.get("fields")
    if isinstance(fields, dict):
        if priority_field_key and priority_field_key in fields:
            raw = _blob_from_value(fields[priority_field_key])
            if raw:
                return raw
        for k, v in fields.items():
            if any(s in str(k).casefold() for s in _PRIORITY_COLUMN_SUBSTRINGS):
                raw = _blob_from_value(v)
                if raw:
                    return raw
    return ""


def _item_decided_at(item: dict) -> datetime | None:
    for key in ("updatedAt", "updated_at", "createdAt", "created_at", "dateCreated", "created"):
        raw = item.get(key)
        if not raw:
            continue
        try:
            s = str(raw).strip()
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            continue
    return None


@dataclass
class _Precedent:
    item_id: str
    tokens: list[str]
    priority: str
    decided_at: datetime | None


@dataclass
class PriorityInference:
    priority: str
    confidence: float
    precedent_count: int
    source: str  # "bm25_precedent" | "insufficient_history"


class _BM25Corpus:
    """Textbook Okapi BM25 over an in-memory document set. Pure term-frequency
    statistics computed fresh from the fetched documents -- no hardcoded vocabulary,
    no trained weights, nothing that survives past this one call."""

    def __init__(self, doc_tokens: list[list[str]], *, k1: float = 1.5, b: float = 0.75) -> None:
        self._k1 = k1
        self._b = b
        self._doc_lens = [len(d) for d in doc_tokens]
        self._avgdl = (sum(self._doc_lens) / len(self._doc_lens)) if doc_tokens else 0.0
        self._term_freqs = [Counter(d) for d in doc_tokens]
        df: Counter[str] = Counter()
        for d in doc_tokens:
            df.update(set(d))
        n = len(doc_tokens)
        self._idf = {t: math.log(1 + (n - freq + 0.5) / (freq + 0.5)) for t, freq in df.items()}

    def score_all(self, query_tokens: list[str]) -> list[float]:
        n = len(self._term_freqs)
        if not n:
            return []
        scores = [0.0] * n
        for i, tf in enumerate(self._term_freqs):
            dl = self._doc_lens[i]
            denom_len = self._k1 * (
                1 - self._b + self._b * (dl / self._avgdl if self._avgdl else 0.0)
            )
            s = 0.0
            for t in query_tokens:
                idf = self._idf.get(t)
                if not idf:
                    continue
                f = tf.get(t, 0)
                if not f:
                    continue
                s += idf * (f * (self._k1 + 1)) / (f + denom_len)
            scores[i] = s
        return scores


def _recency_weight(decided_at: datetime | None, *, now: datetime, half_life_days: float) -> float:
    """Exponential decay: a precedent's vote is worth half as much every `half_life_days`.
    Missing timestamp -> full weight (no basis to discount it)."""
    if decided_at is None or half_life_days <= 0:
        return 1.0
    age_days = max(0.0, (now - decided_at).total_seconds() / 86400.0)
    return 0.5 ** (age_days / half_life_days)


def bm25_consensus(
    *,
    query_title: str,
    query_body: str,
    precedents: list[_Precedent],
    now: datetime,
    top_k: int,
    half_life_days: float,
) -> tuple[str, float, int] | None:
    """Returns (priority, confidence, precedents_considered) or None if nothing scored."""
    usable = [p for p in precedents if p.tokens and p.priority]
    if not usable:
        return None

    corpus = _BM25Corpus([p.tokens for p in usable])
    query_tokens = _tokenize(f"{query_title}\n{query_body}")
    if not query_tokens:
        return None
    scores = corpus.score_all(query_tokens)

    ranked = sorted(range(len(usable)), key=lambda i: -scores[i])[: max(1, top_k)]
    bucket_weight: dict[str, float] = {}
    total_weight = 0.0
    considered = 0
    for i in ranked:
        if scores[i] <= 0:
            continue
        weight = scores[i] * _recency_weight(
            usable[i].decided_at, now=now, half_life_days=half_life_days
        )
        if weight <= 0:
            continue
        bucket_weight[usable[i].priority] = bucket_weight.get(usable[i].priority, 0.0) + weight
        total_weight += weight
        considered += 1

    if total_weight <= 0 or not bucket_weight:
        return None

    winner, winner_weight = max(bucket_weight.items(), key=lambda kv: kv[1])
    # The winning bucket's share of total retrieved weight IS the agreement/confidence
    # measure: a unanimous vote concentrates all weight on one bucket (-> 1.0); a vote
    # split across three buckets spreads it out (-> ~0.33). No separate entropy calc
    # needed -- this ratio already captures retrieval agreement.
    confidence = winner_weight / total_weight
    return winner, confidence, considered


def board_saturation(precedents: list[_Precedent]) -> float:
    """Fraction of fetched board items already at High/Very Important. Confidence
    modifier only -- see module docstring; never used to pick the winning bucket."""
    bearing = [p for p in precedents if p.priority]
    if not bearing:
        return 0.0
    urgent = sum(1 for p in bearing if p.priority in ("High", "Very Important"))
    return urgent / len(bearing)


async def _fetch_precedents(plaky: PlakyClient, board_id: str) -> list[_Precedent]:
    listed = await plaky.list_board_items(board_id, max_pages=settings.pr_linking_board_max_pages)
    if not listed.get("ok"):
        return []
    priority_field_key = await _priority_field_key(board_id)
    out: list[_Precedent] = []
    for item in listed.get("items") or []:
        if not isinstance(item, dict):
            continue
        item_id = _item_id(item)
        if not item_id:
            continue
        title, desc = _item_title_desc(item)
        tokens = _tokenize(f"{title}\n{desc}")
        raw_priority = _item_priority(item, priority_field_key)
        priority = canonical_task_priority(raw_priority, default="") if raw_priority else ""
        if priority not in TASK_PRIORITY_TAGS:
            priority = ""
        out.append(
            _Precedent(
                item_id=item_id,
                tokens=tokens,
                priority=priority,
                decided_at=_item_decided_at(item),
            )
        )
    return out


async def _file_churn_ratio(
    full_name: str, changed_files: list[str], *, lookback_days: float
) -> float | None:
    """Fraction of this repo's recent commit activity that touched the PR's own changed
    files, in the trailing window — a self-normalizing ratio (no fixed magic-number
    threshold): a repo where 80% of recent commits touch these exact files is a real
    hotspot signal; the same raw commit COUNT means nothing on its own without knowing
    how busy the repo is overall. Returns None on any fetch failure (never blocks or
    guesses)."""
    if not changed_files:
        return None
    from datetime import timedelta

    from boardman.github.http import shared_github_client
    from boardman.github.repo_fetch import _parse_owner_repo, github_request

    parsed = _parse_owner_repo(full_name)
    if not parsed:
        return None
    owner, repo = parsed
    since = (datetime.now(UTC) - timedelta(days=max(1.0, lookback_days))).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    base = f"/repos/{owner}/{repo}/commits"
    try:
        client = shared_github_client()
        repo_resp = await github_request(client, f"{base}?since={since}&per_page=100")
        if repo_resp.status_code != 200:
            return None
        repo_commits = repo_resp.json()
        if not isinstance(repo_commits, list) or not repo_commits:
            return None
        total = len(repo_commits)

        touched_shas: set[str] = set()
        for path in changed_files[: max(1, settings.pr_priority_churn_max_files)]:
            r = await github_request(client, f"{base}?path={path}&since={since}&per_page=100")
            if r.status_code != 200:
                continue
            rows = r.json()
            if isinstance(rows, list):
                for c in rows:
                    if isinstance(c, dict):
                        sha = str(c.get("sha") or "")
                        if sha:
                            touched_shas.add(sha)
        if total <= 0:
            return None
        return min(1.0, len(touched_shas) / total)
    except Exception:  # noqa: BLE001 - best-effort confidence modifier only
        _log.debug("file churn ratio lookup failed for %s", full_name, exc_info=True)
        return None


async def churn_confidence_multiplier(full_name: str, changed_files: list[str]) -> float:
    """1.0 = no adjustment. Ambiguous signal either way (could mean "actively unstable"
    or just "busy repo, no real signal"), so it only ever narrows confidence, never
    widens it — same treatment as board saturation, and for the same reason."""
    if not settings.pr_priority_churn_enabled:
        return 1.0
    ratio = await _file_churn_ratio(
        full_name, changed_files, lookback_days=settings.pr_priority_churn_lookback_days
    )
    if ratio is None:
        return 1.0
    return 1.0 - (ratio * 0.3)


async def infer_priority_from_board_precedent(
    plaky: PlakyClient,
    *,
    board_id: str,
    title: str,
    body: str,
) -> PriorityInference | None:
    """BM25-over-live-board-history priority guess, or None if the board doesn't have
    enough priority-bearing history / retrieval was too weak to trust — caller should
    fall back to the rule-based inference in that case."""
    if not settings.pr_priority_precedent_enabled or not board_id:
        return None

    precedents = await _fetch_precedents(plaky, board_id)
    bearing = [p for p in precedents if p.priority]
    if len(bearing) < max(1, settings.pr_priority_precedent_min_corpus):
        return PriorityInference(
            priority="",
            confidence=0.0,
            precedent_count=len(bearing),
            source="insufficient_history",
        )

    now = datetime.now(UTC)
    result = bm25_consensus(
        query_title=title,
        query_body=body,
        precedents=bearing,
        now=now,
        top_k=settings.pr_priority_precedent_top_k,
        half_life_days=settings.pr_priority_precedent_half_life_days,
    )
    if result is None:
        return PriorityInference(
            priority="", confidence=0.0, precedent_count=len(bearing), source="insufficient_history"
        )

    priority, confidence, considered = result

    if settings.pr_priority_board_saturation_enabled:
        saturation = board_saturation(bearing)
        # A saturated board (most items already urgent) widens the plausible margin of
        # error on "this one is ALSO urgent" -- so a saturated board's confidence is
        # damped toward the floor rather than trusted at face value. Purely a
        # confidence adjustment: `priority` above is untouched.
        confidence *= 1.0 - (saturation * 0.3)

    return PriorityInference(
        priority=priority,
        confidence=max(0.0, min(1.0, confidence)),
        precedent_count=considered,
        source="bm25_precedent",
    )


async def infer_priority_for_new_task(
    plaky: PlakyClient,
    *,
    board_id: str,
    title: str,
    body: str,
    labels: list[str],
    full_name: str,
    changed_files: list[str],
) -> str:
    """Priority for a Plaky task about to be created from a PR that matched nothing
    existing. Precedence:
      1. Explicit GitHub priority label — a human already said so.
      2. BM25-over-live-board-history consensus (this module), if the board has enough
         priority-bearing precedent and retrieval was confident enough.
      3. Rule-based text inference (priority_rules.py) as the safety net otherwise.
    """
    from boardman.services.priority_rules import (
        infer_priority_from_text,
        priority_from_github_label,
    )

    for raw in labels:
        explicit = priority_from_github_label(raw)
        if explicit:
            return explicit

    try:
        inference = await infer_priority_from_board_precedent(
            plaky, board_id=board_id, title=title, body=body
        )
    except Exception:  # noqa: BLE001 - precedent retrieval is best-effort, never blocking
        _log.warning("board precedent priority inference failed; falling back", exc_info=True)
        inference = None
    if inference is not None and inference.priority:
        confidence = inference.confidence
        if changed_files:
            confidence *= await churn_confidence_multiplier(full_name, changed_files)
        if confidence >= settings.pr_priority_precedent_confidence_floor:
            return inference.priority

    return infer_priority_from_text(title, body, labels)
