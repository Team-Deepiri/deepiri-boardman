"""GitHunt cold-start enrichment: permanent disk cache (50-call/month free tier), and
the advisory score->qa_tier bucketing. No real network calls."""

from __future__ import annotations

import json

import httpx
import pytest

from boardman.github import githunt_enrichment as ge


def test_no_api_key_returns_none_without_network(tmp_path, monkeypatch):
    monkeypatch.setattr(ge.settings, "githunt_api_key", "")
    monkeypatch.setattr(ge.settings, "githunt_cache_json_path", str(tmp_path / "cache.json"))
    assert ge.fetch_and_cache_profile_sync("alice") is None
    assert not (tmp_path / "cache.json").exists()


def test_successful_lookup_is_cached_to_disk(tmp_path, monkeypatch):
    cache_path = tmp_path / "cache.json"
    monkeypatch.setattr(ge.settings, "githunt_api_key", "ghk_live_test")
    monkeypatch.setattr(ge.settings, "githunt_cache_json_path", str(cache_path))

    calls = {"n": 0}

    def fake_get(self, url, headers=None):
        calls["n"] += 1
        assert headers["Authorization"] == "Bearer ghk_live_test"
        assert url.endswith("/v1/users/alice")
        return httpx.Response(200, json={"data": {"activity_score": 80, "tech_stack_score": 60}})

    monkeypatch.setattr(httpx.Client, "get", fake_get)

    profile = ge.fetch_and_cache_profile_sync("Alice")
    assert profile == {"activity_score": 80, "tech_stack_score": 60}
    assert calls["n"] == 1

    on_disk = json.loads(cache_path.read_text())
    assert on_disk["alice"] == {"activity_score": 80, "tech_stack_score": 60}

    # Second call must not hit the network again -- this is the whole point against a
    # 50-call/month quota.
    profile2 = ge.fetch_and_cache_profile_sync("alice")
    assert profile2 == {"activity_score": 80, "tech_stack_score": 60}
    assert calls["n"] == 1


def test_miss_is_cached_as_none_to_avoid_reburning_quota(tmp_path, monkeypatch):
    cache_path = tmp_path / "cache.json"
    monkeypatch.setattr(ge.settings, "githunt_api_key", "ghk_live_test")
    monkeypatch.setattr(ge.settings, "githunt_cache_json_path", str(cache_path))

    calls = {"n": 0}

    def fake_get(self, url, headers=None):
        calls["n"] += 1
        return httpx.Response(404, json={"message": "not found"})

    monkeypatch.setattr(httpx.Client, "get", fake_get)

    assert ge.fetch_and_cache_profile_sync("ghost") is None
    assert calls["n"] == 1
    assert ge.has_cached("ghost") is True
    assert ge.cached_profile("ghost") is None

    # Cached miss must not re-query.
    assert ge.fetch_and_cache_profile_sync("ghost") is None
    assert calls["n"] == 1


def test_network_error_returns_none_without_caching(tmp_path, monkeypatch):
    cache_path = tmp_path / "cache.json"
    monkeypatch.setattr(ge.settings, "githunt_api_key", "ghk_live_test")
    monkeypatch.setattr(ge.settings, "githunt_cache_json_path", str(cache_path))

    def fake_get(self, url, headers=None):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx.Client, "get", fake_get)

    assert ge.fetch_and_cache_profile_sync("bob") is None
    assert ge.has_cached("bob") is False


@pytest.mark.parametrize(
    "profile,expected",
    [
        (None, None),
        ({}, None),
        ({"activity_score": 90, "tech_stack_score": 92}, 2.82),
        ({"activity_score": 50, "tech_stack_score": 40}, 1.9),
        ({"activity_score": 10, "tech_stack_score": 5}, 1.15),
        ({"activity_score": 90}, 2.8),
        ({"activity_score": 100, "tech_stack_score": 100}, 3.0),
        ({"activity_score": 0, "tech_stack_score": 0}, 1.0),
    ],
)
def test_qa_tier_from_profile_linear_mapping(profile, expected):
    result = ge.qa_tier_from_profile(profile)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)
