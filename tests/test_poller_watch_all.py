"""Which repos the local poller actually watches.

The pinned list named three repos out of an org of 60. `all` watches the org instead --
but only the repos that can genuinely be synchronized, and it says out loud why it skipped
the rest. "no Plaky board resolves for this repo" is the answer to "why isn't my repo
syncing", and that answer must not require reading the source.

Checked against the live org and Plaky catalog on 2026-08-21: of 60 non-archived repos, 43
resolve to a board (one via an explicit repos.yml entry, the rest via a Plaky group named
after the repo) and 17 resolve to nothing and are excluded. Worth recording because two of
them were expected to be ineligible and are not: diri-cyrex is NOT archived on GitHub, and
diva DOES have a Plaky group (board 269030). Neither placement is invented -- both come
from a group that carries the repo's name.

43 repos is also what makes the rate budget matter: at the configured 15s that would be
~62,000 GitHub calls/hour against a 5,000 limit, so the poller throttles itself to 310s.
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


def _with_boards(
    monkeypatch, boards: dict[str, str], sources: dict[str, str] | None = None
) -> None:
    class Routing:
        def __init__(self, board_id: str) -> None:
            self.plaky_board_id = board_id
            self.plaky_group_id = ""

    async def fake_routing(full, _short, _org, with_source=False):
        r = Routing(boards.get(full, ""))
        src = (sources or {}).get(full, "discovered:group_slug_match")
        return (r, src) if with_source else r

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
    # fetch_org_repository_full_names already drops archived repos before we see them,
    # which is what skip_archived=True buys.
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

    async def fake_routing(full, _short, _org, with_source=False):
        if full.endswith("/broken"):
            raise RuntimeError("routing table is malformed for this repo")
        return (Routing(), "explicit") if with_source else Routing()

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


def test_the_interval_stretches_to_fit_the_api_budget() -> None:
    """31 repos at 15s is ~30,000 GitHub calls/hour against a 5,000/hour limit the
    assistant's own tools also spend from."""
    from boardman.services.github_poller import (
        _POLLER_HOURLY_CALL_BUDGET,
        cycle_call_estimate,
    )
    from boardman.services.github_poller import GitHubEventPoller as P

    # Even three repos at 15s is over budget once open PR branches are counted, so it is
    # stretched too -- modestly, and honestly.
    small = P._safe_interval(15.0, 3)
    assert 15.0 < small < 30.0, small
    assert (3600.0 / small) * cycle_call_estimate(3) <= _POLLER_HOURLY_CALL_BUDGET + 1

    stretched = P._safe_interval(15.0, 31)
    assert stretched > 15.0
    calls_per_hour = (3600.0 / stretched) * cycle_call_estimate(31)
    assert calls_per_hour <= _POLLER_HOURLY_CALL_BUDGET + 1, calls_per_hour
    # And it never speeds anything UP: a generous interval is left alone.
    assert P._safe_interval(600.0, 31) == 600.0


def test_the_interval_is_unchanged_when_nothing_is_watched() -> None:
    from boardman.services.github_poller import GitHubEventPoller as P

    assert P._safe_interval(15.0, 0) == 15.0


@pytest.mark.asyncio
async def test_the_org_default_board_is_not_a_destination(monkeypatch, _org) -> None:
    """repos.yml `defaults` answers for EVERY repo, so accepting it while sweeping would
    file all of them onto one shared board and call that a placement."""
    monkeypatch.setattr(settings, "testing_live_plaky_repos", "all")
    _with_org_repos(monkeypatch, ["Team-Deepiri/configured", "Team-Deepiri/unconfigured"])
    _with_boards(
        monkeypatch,
        {"Team-Deepiri/configured": "269031", "Team-Deepiri/unconfigured": "269031"},
        sources={
            "Team-Deepiri/configured": "explicit",
            "Team-Deepiri/unconfigured": "org_default",
        },
    )

    watched, excluded = await gp.resolve_poller_repos()

    assert watched == ["Team-Deepiri/configured"]
    assert excluded == [
        (
            "Team-Deepiri/unconfigured",
            "only the org-default board resolves; no placement of its own",
        )
    ]


@pytest.mark.asyncio
async def test_an_explicit_list_still_honours_an_org_default(monkeypatch, _org) -> None:
    """Naming a repo is a decision. The default is only refused while sweeping."""
    monkeypatch.setattr(settings, "testing_live_plaky_repos", "Team-Deepiri/unconfigured")

    watched, excluded = await gp.resolve_poller_repos()

    assert watched == ["Team-Deepiri/unconfigured"]
    assert excluded == []


def test_a_busy_org_is_priced_by_the_branches_it_actually_has() -> None:
    """The per-cycle cost is not a per-repo constant.

    31 repos each holding five open PRs is 155 extra /commits calls a cycle on top of the
    124 fixed ones -- more than twice what a flat per-repo guess charged. Pricing the
    cycle at the guess let the throttle report a safe interval that spent ~4,500 calls an
    hour: past the poller's own budget, and most of GitHub's whole allowance.
    """
    from boardman.services.github_poller import (
        _POLLER_HOURLY_CALL_BUDGET,
        cycle_call_estimate,
    )
    from boardman.services.github_poller import GitHubEventPoller as P

    busy = cycle_call_estimate(31, 155)
    assert busy > cycle_call_estimate(31), (busy, cycle_call_estimate(31))

    interval = P._safe_interval(15.0, 31, 155)
    assert (3600.0 / interval) * busy <= _POLLER_HOURLY_CALL_BUDGET + 1

    # And a quiet org is not overcharged for PRs it does not have: an observed zero is a
    # real number, and buys a faster loop than the pre-observation allowance.
    assert cycle_call_estimate(31, 0) < cycle_call_estimate(31)
    assert P._safe_interval(15.0, 31, 0) < P._safe_interval(15.0, 31)


@pytest.mark.asyncio
async def test_the_interval_follows_the_open_prs_that_come_and_go() -> None:
    """Resolved once at startup, the interval would keep pricing a cycle from a PR count
    that stopped being true the moment somebody opened one."""
    from boardman.services.github_poller import GitHubEventPoller, track_pr_branch

    poller = GitHubEventPoller()
    repos = [f"Team-Deepiri/repo-{i}" for i in range(31)]
    assert poller._tracked_branch_count(repos) == 0

    for repo in repos:
        branches: dict[int, str] = {}
        for pr in range(5):
            track_pr_branch(branches, pr, f"feat/branch-{pr}", "open")
        poller._processed[repo] = {"pr_branches": branches}

    assert poller._tracked_branch_count(repos) == 155
    busy = poller._safe_interval(15.0, len(repos), poller._tracked_branch_count(repos))

    # PRs merge, branches are forgotten, and the loop is allowed to speed back up.
    for repo in repos:
        for pr in range(5):
            track_pr_branch(
                poller._processed[repo]["pr_branches"], pr, f"feat/branch-{pr}", "closed"
            )
    assert poller._tracked_branch_count(repos) == 0
    assert poller._safe_interval(15.0, len(repos), 0) < busy


@pytest.mark.asyncio
async def test_a_pr_reopened_while_the_poller_was_down_is_still_noticed() -> None:
    """Which PRs closed is in-process memory, and a restart loses it.

    A PR reopened in the meantime emitted nothing at all: its links stayed withdrawn, so
    `has_any_open_pr_for_task` reported no open PR and merge-gated completion could finish
    the task while that PR was still open. The registry remembers what the set forgets.
    """
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from boardman.database.models import Base, PullRequestTaskLink
    from boardman.services.pr_task_registry import withdrawn_pr_numbers

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as session:
        session.add_all(
            [
                PullRequestTaskLink(
                    github_repo="deepiri-boardman",
                    github_pr_number=88,
                    github_issue_number=94,
                    plaky_task_id="7209283",
                    link_source="issue_keyword",
                    withdrawn_at=datetime(2026, 8, 21, 3, 0, 0),
                ),
                PullRequestTaskLink(
                    github_repo="deepiri-boardman",
                    github_pr_number=89,
                    github_issue_number=95,
                    plaky_task_id="7209999",
                    link_source="issue_keyword",
                ),
                # Retired on purpose when the PR re-pointed at another issue. Reopening
                # does not un-say which issue a PR closes, so this must not look like one.
                PullRequestTaskLink(
                    github_repo="deepiri-boardman",
                    github_pr_number=90,
                    github_issue_number=0,
                    plaky_task_id="7183844",
                    link_source="superseded_by_issue_link",
                    withdrawn_at=datetime(2026, 8, 21, 3, 0, 0),
                ),
            ]
        )
        await session.commit()

        withdrawn = await withdrawn_pr_numbers(session, github_repo="deepiri-boardman")

        assert 88 in withdrawn, "the PR whose links a close retired"
        assert 89 not in withdrawn, "a PR that was never closed"
        assert 90 not in withdrawn, "retired on purpose; reopening does not un-say it"
    await engine.dispose()


def test_the_throttle_explains_itself_once_per_answer() -> None:
    """`_safe_interval` runs every cycle, so an unconditional warning repeated the same
    four lines every 22 seconds for the life of the process, which is how a real warning
    stops being read."""
    import logging

    from boardman.services import github_poller as gp
    from boardman.services.github_poller import GitHubEventPoller as P

    gp._INTERVAL_WARNED.clear()
    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = Capture()
    gp._log.addHandler(handler)
    try:
        for _ in range(5):
            assert P._safe_interval(15.0, 31) > 15.0
        assert len([r for r in records if r.levelno >= logging.WARNING]) == 1

        # A different answer is worth saying once too.
        P._safe_interval(15.0, 12)
        assert len([r for r in records if r.levelno >= logging.WARNING]) == 2
    finally:
        gp._log.removeHandler(handler)
        gp._INTERVAL_WARNED.clear()
