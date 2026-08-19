from __future__ import annotations

from boardman.agent.repo_resolution import resolve_repo
from boardman.repos_config import RepoRouting


def _registered(monkeypatch, values: dict[str, RepoRouting]) -> None:
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "github_org", "deepiri")
    monkeypatch.setattr(bs.settings, "github_bare_repo_owner", "deepiri")
    monkeypatch.setattr("boardman.agent.repo_resolution.list_registered_repos", lambda: values)


def test_explicit_repo_wins_over_message_and_session(monkeypatch) -> None:
    _registered(monkeypatch, {"boardman": RepoRouting()})

    result = resolve_repo(
        explicit_repo="deepiri/boardman",
        session_repo="deepiri/other",
        message="look at deepiri/another",
    )

    assert result.repo == "deepiri/boardman"
    assert result.source == "explicit"


def test_configured_message_mention_switches_session_repo(monkeypatch) -> None:
    _registered(
        monkeypatch,
        {"cyrex": RepoRouting(), "sorge": RepoRouting()},
    )

    result = resolve_repo(
        explicit_repo=None,
        session_repo="deepiri/boardman",
        message="what is the current status of cyrex?",
    )

    assert result.repo == "deepiri/cyrex"
    assert result.source == "message"


def test_session_repo_is_used_for_follow_up_without_repeating_repo(monkeypatch) -> None:
    _registered(monkeypatch, {"boardman": RepoRouting()})

    result = resolve_repo(
        explicit_repo=None,
        session_repo="deepiri/boardman",
        message="what should I do next?",
    )

    assert result.repo == "deepiri/boardman"
    assert result.source == "session"


def test_unknown_named_repo_does_not_use_single_repo_fallback(monkeypatch) -> None:
    _registered(monkeypatch, {"boardman": RepoRouting()})

    result = resolve_repo(
        explicit_repo=None,
        session_repo=None,
        message="create a task for unknown-project",
    )

    assert result.repo is None
    assert result.source == "unknown-mentioned"


def test_generic_this_repo_and_task_count_keep_single_repo_fallback(monkeypatch) -> None:
    _registered(monkeypatch, {"boardman": RepoRouting()})

    for message in ("what is the status for this repo?", "show me 5 tasks"):
        result = resolve_repo(explicit_repo=None, session_repo=None, message=message)
        assert result.repo == "deepiri/boardman"
        assert result.source == "single-configured"


def test_ambiguous_configured_mentions_are_not_silently_routed(monkeypatch) -> None:
    _registered(
        monkeypatch,
        {"cyrex": RepoRouting(), "sorge": RepoRouting()},
    )

    result = resolve_repo(
        explicit_repo=None,
        session_repo=None,
        message="scan cyrex and sorge",
    )

    assert result.repo is None
    assert result.source == "ambiguous"
    assert set(result.candidates) == {"deepiri/cyrex", "deepiri/sorge"}
