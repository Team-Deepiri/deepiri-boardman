"""The boardman.github.auth seam: mode selection, availability, App fallback."""

from __future__ import annotations

import pytest

from boardman.github import auth
from boardman.github.app_auth import GitHubAppAuthError


@pytest.fixture
def set_mode(monkeypatch):
    import boardman.settings as bs

    def _set(mode: str, *, pat: str = "", app: bool = False):
        monkeypatch.setattr(bs.settings, "github_auth_mode", mode)
        monkeypatch.setattr(bs.settings, "github_pat", pat)
        monkeypatch.setattr(bs.settings, "github_app_id", "1" if app else "")
        monkeypatch.setattr(bs.settings, "github_app_installation_id", "2" if app else "")
        monkeypatch.setattr(bs.settings, "github_app_private_key", "pem" if app else "")

    return _set


# ---- github_auth_available ------------------------------------------------
def test_available_pat_mode(set_mode):
    set_mode("pat", pat="ghp_x")
    assert auth.github_auth_available() is True
    set_mode("pat", pat="")
    assert auth.github_auth_available() is False


def test_available_github_app_mode_needs_app_creds(set_mode):
    set_mode("github_app", pat="ghp_x", app=False)
    assert auth.github_auth_available() is False
    set_mode("github_app", pat="", app=True)
    assert auth.github_auth_available() is True


def test_available_both_mode_either_credential(set_mode):
    set_mode("both", pat="ghp_x", app=False)
    assert auth.github_auth_available() is True
    set_mode("both", pat="", app=True)
    assert auth.github_auth_available() is True
    set_mode("both", pat="", app=False)
    assert auth.github_auth_available() is False


# ---- github_auth_header --------------------------------------------------
async def test_header_pat_mode_is_pure(set_mode):
    set_mode("pat", pat="ghp_tok")
    hdr = await auth.github_auth_header()
    assert hdr["Authorization"] == "Bearer ghp_tok"
    assert hdr["Accept"] == "application/vnd.github+json"


async def test_header_github_app_mode_uses_installation_token(set_mode, monkeypatch):
    set_mode("github_app", app=True)

    async def fake_token() -> str:
        return "ghs_installation"

    monkeypatch.setattr(auth, "get_installation_token", fake_token)
    hdr = await auth.github_auth_header()
    assert hdr["Authorization"] == "Bearer ghs_installation"


async def test_header_both_mode_falls_back_to_pat_on_mint_failure(set_mode, monkeypatch):
    set_mode("both", pat="ghp_fallback", app=True)

    async def boom() -> str:
        raise GitHubAppAuthError("no key")

    monkeypatch.setattr(auth, "get_installation_token", boom)
    hdr = await auth.github_auth_header()
    assert hdr["Authorization"] == "Bearer ghp_fallback"


async def test_header_github_app_mode_raises_when_mint_fails_and_no_fallback(set_mode, monkeypatch):
    set_mode("github_app", app=True)

    async def boom() -> str:
        raise GitHubAppAuthError("no key")

    monkeypatch.setattr(auth, "get_installation_token", boom)
    with pytest.raises(GitHubAppAuthError):
        await auth.github_auth_header()


def test_header_sync_pat_mode(set_mode):
    set_mode("pat", pat="ghp_sync")
    hdr = auth.github_auth_header_sync()
    assert hdr["Authorization"] == "Bearer ghp_sync"
