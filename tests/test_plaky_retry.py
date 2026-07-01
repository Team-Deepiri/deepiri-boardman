"""Transient-failure retry for Plaky writes (network errors + 5xx on idempotent methods)."""

from __future__ import annotations

import httpx
import pytest

import boardman.plaky.client as client_mod
from boardman.plaky.client import _request_with_rate_limit_retry


class _Resp:
    def __init__(self, status_code: int):
        self.status_code = status_code
        self.headers: dict = {}
        self.text = ""

    def json(self):
        return {}


class _FakeClient:
    """Returns/raises a scripted sequence of outcomes; counts calls."""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = 0

    async def request(self, **kwargs):
        self.calls += 1
        outcome = self._outcomes[min(self.calls - 1, len(self._outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch):
    # No real sleeping in tests.
    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(client_mod.asyncio, "sleep", _noop)


@pytest.mark.asyncio
async def test_patch_retries_on_transient_5xx_then_succeeds():
    fake = _FakeClient([_Resp(503), _Resp(200)])
    r = await _request_with_rate_limit_retry(fake, "PATCH", "http://x", {}, retries=2)
    assert r.status_code == 200
    assert fake.calls == 2


@pytest.mark.asyncio
async def test_patch_retries_on_network_error_then_succeeds():
    fake = _FakeClient([httpx.ConnectError("boom"), _Resp(200)])
    r = await _request_with_rate_limit_retry(fake, "PATCH", "http://x", {}, retries=2)
    assert r.status_code == 200
    assert fake.calls == 2


@pytest.mark.asyncio
async def test_patch_gives_up_after_retries_returns_last_5xx():
    fake = _FakeClient([_Resp(503), _Resp(503), _Resp(503)])
    r = await _request_with_rate_limit_retry(fake, "PATCH", "http://x", {}, retries=2)
    assert r.status_code == 503
    assert fake.calls == 3  # initial + 2 retries


@pytest.mark.asyncio
async def test_post_is_not_retried_on_5xx():
    # POST (create) must not auto-retry — avoid double-create.
    fake = _FakeClient([_Resp(503), _Resp(200)])
    r = await _request_with_rate_limit_retry(fake, "POST", "http://x", {}, retries=2)
    assert r.status_code == 503
    assert fake.calls == 1


@pytest.mark.asyncio
async def test_post_network_error_raises_not_retried():
    fake = _FakeClient([httpx.ConnectError("boom"), _Resp(200)])
    with pytest.raises(httpx.ConnectError):
        await _request_with_rate_limit_retry(fake, "POST", "http://x", {}, retries=2)
    assert fake.calls == 1


@pytest.mark.asyncio
async def test_success_returns_immediately_no_retry():
    fake = _FakeClient([_Resp(200)])
    r = await _request_with_rate_limit_retry(fake, "GET", "http://x", {}, retries=2)
    assert r.status_code == 200
    assert fake.calls == 1


@pytest.mark.asyncio
async def test_429_still_retried():
    fake = _FakeClient([_Resp(429), _Resp(200)])
    r = await _request_with_rate_limit_retry(fake, "PATCH", "http://x", {}, retries=2)
    assert r.status_code == 200
    assert fake.calls == 2
