"""BM25 precedent-based priority inference — pure logic + a live-board-shaped fake."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from boardman.services.priority_precedent import (
    _BM25Corpus,
    _Precedent,
    _recency_weight,
    _tokenize,
    bm25_consensus,
    board_saturation,
    infer_priority_from_board_precedent,
)


def test_tokenize_is_generic_word_split():
    assert _tokenize("Fix the Auth-Bypass bug!") == ["fix", "the", "auth", "bypass", "bug"]


def test_tokenize_empty():
    assert _tokenize("") == []
    assert _tokenize(None) == []


def test_bm25_scores_more_relevant_doc_higher():
    corpus = _BM25Corpus(
        [
            ["fix", "authentication", "bypass", "security"],
            ["update", "readme", "typo"],
            ["rename", "variable", "cleanup"],
        ]
    )
    scores = corpus.score_all(["authentication", "bypass"])
    assert scores[0] > scores[1]
    assert scores[0] > scores[2]


def test_bm25_empty_corpus_scores_nothing():
    corpus = _BM25Corpus([])
    assert corpus.score_all(["anything"]) == []


def test_recency_weight_decays_with_age():
    now = datetime.now(UTC)
    fresh = _recency_weight(now, now=now, half_life_days=30)
    old = _recency_weight(now - timedelta(days=30), now=now, half_life_days=30)
    very_old = _recency_weight(now - timedelta(days=300), now=now, half_life_days=30)
    assert fresh == pytest.approx(1.0)
    assert old == pytest.approx(0.5, abs=0.01)
    assert very_old < old


def test_recency_weight_missing_timestamp_gets_full_weight():
    now = datetime.now(UTC)
    assert _recency_weight(None, now=now, half_life_days=30) == 1.0


def test_bm25_consensus_picks_bucket_with_more_weighted_agreement():
    now = datetime.now(UTC)
    precedents = [
        _Precedent("1", ["security", "vulnerability", "auth"], "Very Important", now),
        _Precedent("2", ["security", "auth", "bypass"], "Very Important", now),
        _Precedent("3", ["update", "readme"], "Low", now),
    ]
    result = bm25_consensus(
        query_title="security auth bypass",
        query_body="",
        precedents=precedents,
        now=now,
        top_k=5,
        half_life_days=120,
    )
    assert result is not None
    priority, confidence, considered = result
    assert priority == "Very Important"
    assert confidence > 0.5
    assert considered >= 1


def test_bm25_consensus_no_query_tokens_returns_none():
    now = datetime.now(UTC)
    precedents = [_Precedent("1", ["a", "b"], "High", now)]
    assert (
        bm25_consensus(
            query_title="",
            query_body="",
            precedents=precedents,
            now=now,
            top_k=5,
            half_life_days=120,
        )
        is None
    )


def test_bm25_consensus_no_matching_terms_returns_none():
    now = datetime.now(UTC)
    precedents = [_Precedent("1", ["apple", "banana"], "High", now)]
    assert (
        bm25_consensus(
            query_title="zebra giraffe",
            query_body="",
            precedents=precedents,
            now=now,
            top_k=5,
            half_life_days=120,
        )
        is None
    )


def test_board_saturation_fraction_of_urgent_items():
    now = datetime.now(UTC)
    precedents = [
        _Precedent("1", [], "High", now),
        _Precedent("2", [], "Very Important", now),
        _Precedent("3", [], "Low", now),
        _Precedent("4", [], "Medium", now),
    ]
    assert board_saturation(precedents) == pytest.approx(0.5)


def test_board_saturation_no_precedents_is_zero():
    assert board_saturation([]) == 0.0


@pytest.mark.asyncio
async def test_insufficient_history_returns_signal_not_a_guess(monkeypatch):
    from boardman.settings import settings

    monkeypatch.setattr(settings, "pr_priority_precedent_enabled", True)
    monkeypatch.setattr(settings, "pr_priority_precedent_min_corpus", 5)

    class FakePlaky:
        async def list_board_items(self, *a, **k):
            return {
                "ok": True,
                "items": [
                    {
                        "id": "1",
                        "title": "Fix auth bug",
                        "priority": "High",
                        "createdAt": "2026-01-01T00:00:00Z",
                    }
                ],
            }

    result = await infer_priority_from_board_precedent(
        FakePlaky(), board_id="b1", title="Fix auth bug again", body=""
    )
    assert result is not None
    assert result.source == "insufficient_history"
    assert result.priority == ""


@pytest.mark.asyncio
async def test_disabled_returns_none(monkeypatch):
    from boardman.settings import settings

    monkeypatch.setattr(settings, "pr_priority_precedent_enabled", False)

    class FakePlaky:
        async def list_board_items(self, *a, **k):
            raise AssertionError("should not be called when disabled")

    result = await infer_priority_from_board_precedent(
        FakePlaky(), board_id="b1", title="anything", body=""
    )
    assert result is None
