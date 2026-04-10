"""Rank Plaky boards/groups by how well their names match a user phrase (API returns id + name)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


def _norm(s: str) -> str:
    return " ".join(s.casefold().split())


def _rank_name_against_query(name: str, query: str, query_tokens: List[str]) -> int:
    """Higher = better match. 0 = no meaningful match."""
    n = _norm(name)
    q = _norm(query)
    if not n or not q:
        return 0
    if q == n:
        return 1000
    if q in n:
        return 700
    if n in q:
        return 550
    if query_tokens:
        if all(t in n for t in query_tokens):
            return 400 + 20 * len(query_tokens)
        overlap = sum(1 for t in query_tokens if t in n)
        return overlap * 100
    return 0


def rank_plaky_rows(
    rows: List[Dict[str, Any]],
    query: str,
    *,
    id_key: str = "id",
    name_key: str = "name",
) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """
    Sort rows by name match to `query`. Each output row: id, name, score (int).

    `best` is set when the top score is strong enough to treat as an automatic pick (>= 400).
    """
    qstrip = (query or "").strip()
    q_tokens = [t for t in _norm(qstrip).split() if len(t) > 1]

    ranked: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        rid = str(row.get(id_key) or "")
        raw_name = str(row.get(name_key) or "")
        score = 0 if not qstrip else _rank_name_against_query(raw_name, qstrip, q_tokens)
        ranked.append({"id": rid, "name": raw_name, "score": score})

    ranked.sort(key=lambda x: (-x["score"], x["name"].casefold()))

    best: Optional[Dict[str, Any]] = None
    if ranked and ranked[0]["score"] >= 400:
        best = dict(ranked[0])

    return ranked, best
