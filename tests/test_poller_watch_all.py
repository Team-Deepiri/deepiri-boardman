"""Which repos the local poller actually watches.

The pinned three-repo list included one archived repo and one with no Plaky board, so two
thirds of every poll cycle was spent on repos that could never sync. `all` watches the org
instead -- but only the repos that can genuinely be synchronized, and it says out loud why
it skipped the rest. "diri-cyrex is archived" is the answer to "why isn't my repo
syncing", and that answer must not require reading the source.
"""

from __future__ import annotations

import pytest

from boardman.services import github_poller as gp
from boardman.settings import settings


@pytest.fixture()
def _org(monkeypatch):
    monkeypatch.setattr(settings, "github_org", "Team-Deepiri")
    monkeypatch.setattr(settings, "github_pat", "t")
    monkeypatch.setattr("boardman.github.http.github_http_client", lambda: object())
    yield


def _with_org_repos(monkeypatch, names: list[str]) -> None:
    async def fake_names(_client, _org, *, skip_archived=True):
        assert skip_archived is True, "an archived repo can never receive new activity"
        return list(names)

    monkeypatch.setattr("boardman.github.org_repos.fetch_org_repository_full_names", fake_names)


def _with_boards(monkeypatch, boards: dict[str, str]) -> None:
    class Routing:
        def __init__(self, board_id: str) -> None:
            self.plaky_board_id = board_id
            self.plaky_group_id = ""

    async def fake_routing(full, _short, _org):
        return Routing(boards.get(full, ""))

    monkeypatch.setattr("boardman.repos_config.get_routing_async", fake_routing)


@pytest.mark.parametrize("token", ["all", "ALL", "*", "auto", " all "])
def test_the_watch_all_tokens_are_recognised(monkeypatch, token: str) -> None:
    monkeypatch.setattr(settings, "testing_live_plaky_repos", token)
    assert gp.watch_all_requested() is True
    assert gp.poller_repos() == [], "watch-all is not a repo named 'all'"


def test_an_explicit_list_is_not_watch_all(monkeypatch) -> None:
    monkeypatch.setattr(settings, "testing_live_plaky_repos", "o/a,o/b")
    assert gp.watch_all_requested() is False
    assert gp.poller_repos() == ["o/a", "o/b"]


@pytest.mark.asyncio
async def test_an_explicit_list_is_honoured_untouched(monkeypatch, _org) -> None:
    """Naming a repo is a decision; filtering it would make the setting useless."""
    monkeypatch.setattr(settings, "testing_live_plaky_repos", "o/a,o/b")

    watched, excluded = await gp.resolve_poller_repos()

    assert watched == ["o/a", "o/b"]
    assert excluded == []


@pytest.mark.asyncio
async def test_watch_all_keeps_only_repos_that_can_actually_sync(monkeypatch, _org) -> None:
    monkeypatch.setattr(settings, "testing_live_plaky_repos", "all")
    # fetch_org_repository_full_names already drops archived repos, so diri-cyrex is
    # simply absent here -- that is what skip_archived=True buys.
    _with_org_repos(
        monkeypatch, ["Team-Deepiri/deepiri-boardman", "Team-Deepiri/diva", "Team-Deepiri/aar"]
    )
    _with_boards(
        monkeypatch,
        {"Team-Deepiri/deepiri-boardman": "269031", "Team-Deepiri/aar": "269099"},
    )

    watched, excluded = await gp.resolve_poller_repos()

    assert watched == ["Team-Deepiri/aar", "Team-Deepiri/deepiri-boardman"]
    assert excluded == [("Team-Deepiri/diva", "no Plaky board resolves for this repo")]


@pytest.mark.asyncio
async def test_a_repo_without_a_board_is_never_given_one(monkeypatch, _org) -> None:
    """A task written to the wrong board is worse than a repo visibly not watched."""
    monkeypatch.setattr(settings, "testing_live_plaky_repos", "all")
    _with_org_repos(monkeypatch, ["Team-Deepiri/diva"])
    _with_boards(monkeypatch, {})

    watched, excluded = await gp.resolve_poller_repos()

    assert watched == []
    assert len(excluded) == 1 and "no Plaky board" in excluded[0][1]


@pytest.mark.asyncio
async def test_one_broken_repo_does_not_stop_the_fleet(monkeypatch, _org) -> None:
    monkeypatch.setattr(settings, "testing_live_plaky_repos", "all")
    _with_org_repos(monkeypatch, ["Team-Deepiri/good", "Team-Deepiri/broken"])

    class Routing:
        plaky_board_id = "269031"
        plaky_group_id = ""

    async def fake_routing(full, _short, _org):
        if full.endswith("/broken"):
            raise RuntimeError("routing table is malformed for this repo")
        return Routing()

    monkeypatch.setattr("boardman.repos_config.get_routing_async", fake_routing)

    watched, excluded = await gp.resolve_poller_repos()

    assert watched == ["Team-Deepiri/good"]
    assert excluded == [("Team-Deepiri/broken", "routing lookup failed: RuntimeError")]


@pytest.mark.asyncio
async def test_a_failed_org_listing_reports_why_rather_than_crashing(monkeypatch, _org) -> None:
    monkeypatch.setattr(settings, "testing_live_plaky_repos", "all")

    async def boom(*_a, **_k):
        raise RuntimeError("GitHub is down")

    monkeypatch.setattr("boardman.github.org_repos.fetch_org_repository_full_names", boom)

    watched, excluded = await gp.resolve_poller_repos()

    assert watched == []
    assert excluded and "could not list" in excluded[0][1]


@pytest.mark.asyncio
async def test_watch_all_without_credentials_says_so(monkeypatch) -> None:
    monkeypatch.setattr(settings, "testing_live_plaky_repos", "all")
    monkeypatch.setattr(settings, "github_org", "")
    monkeypatch.setattr(settings, "github_pat", "")

    watched, excluded = await gp.resolve_poller_repos()

    assert watched == []
    assert "GITHUB_ORG and GITHUB_PAT" in excluded[0][1]


def test_watch_all_starts_the_poller(monkeypatch) -> None:
    """The empty-list guard must not refuse to start when the config says 'all'."""
    monkeypatch.setattr(settings, "testing_live_plaky", True)
    monkeypatch.setattr(settings, "testing_live_plaky_repos", "all")
    started: list[bool] = []

    class FakePoller:
        def start(self):
            started.append(True)

    monkeypatch.setattr(gp, "_poller", FakePoller())
    assert gp.start_github_poller_if_enabled() is not None
    assert started == [True]


def test_an_empty_repo_setting_still_refuses_to_start(monkeypatch) -> None:
    monkeypatch.setattr(settings, "testing_live_plaky", True)
    monkeypatch.setattr(settings, "testing_live_plaky_repos", "")

    assert gp.start_github_poller_if_enabled() is None
