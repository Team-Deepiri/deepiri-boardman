"""GitHub App installation-token minting, caching and refresh."""

from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from boardman.github import app_auth
from boardman.github.app_auth import (
    GitHubAppAuthError,
    _CachedToken,
    _mint_app_jwt,
    _parse_exchange_response,
    get_installation_token,
)


def _rsa_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


@pytest.fixture(autouse=True)
def _clear_cache():
    app_auth._clear_cache_for_tests()
    yield
    app_auth._clear_cache_for_tests()


@pytest.fixture
def app_configured(monkeypatch):
    import boardman.settings as bs

    pem = _rsa_pem()
    monkeypatch.setattr(bs.settings, "github_app_id", "123456")
    monkeypatch.setattr(bs.settings, "github_app_installation_id", "7890")
    monkeypatch.setattr(bs.settings, "github_app_private_key", pem)
    return pem


def test_mint_app_jwt_is_valid_rs256(app_configured):
    token = _mint_app_jwt()
    # Decode without verifying exp/aud but do verify the signature with the public key.
    pub = serialization.load_pem_private_key(app_configured.encode(), password=None).public_key()
    claims = jwt.decode(token, pub, algorithms=["RS256"], options={"verify_exp": False})
    assert claims["iss"] == "123456"
    assert claims["exp"] - claims["iat"] <= 10 * 60


def test_mint_app_jwt_restores_escaped_newlines(monkeypatch, app_configured):
    import boardman.settings as bs

    single_line = app_configured.replace("\n", "\\n")
    assert "\n" not in single_line
    monkeypatch.setattr(bs.settings, "github_app_private_key", single_line)
    # Should not raise — the key is usable once newlines are restored.
    _mint_app_jwt()


def test_mint_app_jwt_missing_config_raises(monkeypatch):
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "github_app_id", "")
    monkeypatch.setattr(bs.settings, "github_app_private_key", "")
    with pytest.raises(GitHubAppAuthError):
        _mint_app_jwt()


def test_parse_exchange_response_ok():
    ct = _parse_exchange_response(201, "", {"token": "ghs_abc", "expires_at": "irrelevant"})
    assert ct.token == "ghs_abc"
    # good_until is ~55 min out (1h minus the 5-min refresh margin).
    assert 3000 < (ct.good_until - time.time()) < 3600


def test_parse_exchange_response_error_status():
    with pytest.raises(GitHubAppAuthError):
        _parse_exchange_response(403, "forbidden", {})


def test_parse_exchange_response_no_token():
    with pytest.raises(GitHubAppAuthError):
        _parse_exchange_response(201, "", {})


async def test_get_installation_token_caches(monkeypatch, app_configured):
    calls = 0

    async def fake_exchange(app_jwt: str) -> _CachedToken:
        nonlocal calls
        calls += 1
        return _CachedToken(token=f"tok{calls}", good_until=time.time() + 3600)

    monkeypatch.setattr(app_auth, "_exchange_jwt_for_installation_token", fake_exchange)

    first = await get_installation_token()
    second = await get_installation_token()
    assert first == second == "tok1"
    assert calls == 1


async def test_get_installation_token_refreshes_when_stale(monkeypatch, app_configured):
    calls = 0

    async def fake_exchange(app_jwt: str) -> _CachedToken:
        nonlocal calls
        calls += 1
        # First token is already past its good_until; second is fresh.
        good_until = time.time() - 1 if calls == 1 else time.time() + 3600
        return _CachedToken(token=f"tok{calls}", good_until=good_until)

    monkeypatch.setattr(app_auth, "_exchange_jwt_for_installation_token", fake_exchange)

    assert await get_installation_token() == "tok1"
    assert await get_installation_token() == "tok2"
    assert calls == 2
