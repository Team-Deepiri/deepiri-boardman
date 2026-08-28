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


def test_known_repo_keys_overrides_yaml_registry(monkeypatch) -> None:
    """When a live org repo listing is passed in, matching must use it — not the yaml-only
    registry — so a repo added on GitHub but not (yet) in repos.yml still resolves, and one
    only ever in repos.yml (removed from GitHub) does not."""
    _registered(monkeypatch, {"stale-yaml-only-repo": RepoRouting()})

    result = resolve_repo(
        explicit_repo=None,
        session_repo=None,
        message="what's the status of the live-only-repo?",
        known_repo_keys=["deepiri/live-only-repo"],
    )

    assert result.repo == "deepiri/live-only-repo"
    assert result.source == "message"

    # The yaml-only entry must not resolve once a live list is supplied.
    result2 = resolve_repo(
        explicit_repo=None,
        session_repo=None,
        message="what's the status of stale-yaml-only-repo?",
        known_repo_keys=["deepiri/live-only-repo"],
    )
    assert result2.repo != "deepiri/stale-yaml-only-repo"


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
