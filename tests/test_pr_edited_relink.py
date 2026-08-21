"""A PR that gains `Fixes #N` after it was opened must link to that issue's task.

Reproduced against the live board: issue #94 already owned Plaky task 7209283, PR #88 was
opened before it referenced #94, and editing the PR body to add `Fixes #94` changed
nothing. `handle_pr_edited` treated any already-linked PR as metadata-only and never
re-read the relationship, so the link could only ever be established at open time.

GitHub is authoritative for the relationship and an author states it whenever they state
it. These tests pin every shape of that: gained, unchanged, moved, replayed, written as a
URL, and implied by a branch name.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from boardman.database.models import Base, IssueTaskMap, PullRequestTaskLink, SyncLog
from boardman.github.webhooks import PullRequestEventPayload
from boardman.services import pr_handler as ph

REPO = "deepiri-boardman"
FULL = "Team-Deepiri/deepiri-boardman"
TASK_94 = "7209283"
TASK_95 = "7209999"


@pytest.fixture()
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture()
def workflow(monkeypatch):
    """Stub every Plaky write the link pipeline performs, and record that it ran."""
    calls: dict[str, list[Any]] = {
        "comments": [],
        "type_and_assignee": [],
        "qa": [],
        "needs_qa": [],
        "updates": [],
    }

    class FakePlaky:
        async def add_comment(self, task_id, body, *, board_id=None):
            calls["comments"].append((task_id, body))
            return {"ok": True}

    class Routing:
        plaky_board_id = "269031"
        plaky_group_id = "g1"
        plaky_table = ""
        category = ""

    async def fake_routing(*_a, **_k):
        return Routing()

    async def fake_type_and_assignee(_plaky, *, task_id, **_k):
        calls["type_and_assignee"].append(task_id)
        return {}

    async def fake_qa(_plaky, *, task_id, **_k):
        calls["qa"].append(task_id)
        return {"assigned": True}

    async def fake_needs_qa(_plaky, task_id, is_draft, board_id, *, allow_regression=True):
        calls["needs_qa"].append((task_id, is_draft, allow_regression))
        return {"ok": True}

    async def fake_update(task_id, inp):
        calls["updates"].append((task_id, inp))
        return {"ok": True}

    monkeypatch.setattr(ph, "PlakyClient", lambda *a, **k: FakePlaky())
    monkeypatch.setattr("boardman.repos_config.get_routing_async", fake_routing)
    monkeypatch.setattr(ph, "_apply_pr_type_and_assignee", fake_type_and_assignee)
    monkeypatch.setattr(ph, "_assign_qa_for_pr", fake_qa)
    monkeypatch.setattr(ph, "_maybe_set_needs_qa", fake_needs_qa)
    monkeypatch.setattr(ph, "update_task_internal", fake_update)
    return calls


def _pr(body: str, *, number: int = 88, title: str = "Add retries", head_ref: str = "feat/x"):
    return PullRequestEventPayload(
        action="edited",
        pull_request={
            "number": number,
            "title": title,
            "body": body,
            "html_url": f"https://github.com/{FULL}/pull/{number}",
            "state": "open",
            "merged": False,
            "draft": False,
            "user": {"login": "ali-ferris"},
            "head": {"ref": head_ref},
        },
        repository={"full_name": FULL, "name": REPO},
    )


async def _seed_issue_task(session, issue_number: int, task_id: str) -> None:
    session.add(
        IssueTaskMap(
            github_repo=REPO,
            github_issue_number=issue_number,
            plaky_task_id=task_id,
            plaky_task_url=f"https://app.plaky.com/i/{task_id}",
        )
    )
    await session.commit()


async def _links(session) -> list[PullRequestTaskLink]:
    rows = await session.execute(
        select(PullRequestTaskLink).where(PullRequestTaskLink.github_repo == REPO)
    )
    return list(rows.scalars())


@pytest.mark.asyncio
async def test_pr_edited_to_add_fixes_links_to_the_existing_issue_task(
    db_session, workflow
) -> None:
    """The reproduction: PR opened without a reference, later edited to add `Fixes #94`."""
    await _seed_issue_task(db_session, 94, TASK_94)

    res = await ph.reconcile_pr_issue_links(_pr("Fixes #94"), db_session)

    assert res["changed"] is True
    assert res["linked"] == [{"issue": 94, "task_id": TASK_94}]
    links = await _links(db_session)
    assert len(links) == 1, "exactly one link row, no duplicate task"
    assert links[0].github_issue_number == 94
    assert links[0].plaky_task_id == TASK_94
    assert links[0].link_source == "issue_keyword"
    # The full PR workflow ran, not just the link row.
    assert workflow["type_and_assignee"] == [TASK_94]
    assert workflow["qa"] == [TASK_94]
    assert workflow["needs_qa"] == [
        (TASK_94, False, False)
    ], "a late link must not ask for QA on a task that is already past it"
    assert len(workflow["comments"]) == 1


@pytest.mark.asyncio
async def test_the_same_edit_delivered_twice_changes_nothing(db_session, workflow) -> None:
    """Webhook delivery is at-least-once; the second one must be a no-op."""
    await _seed_issue_task(db_session, 94, TASK_94)
    payload = _pr("Fixes #94")

    first = await ph.reconcile_pr_issue_links(payload, db_session)
    second = await ph.reconcile_pr_issue_links(payload, db_session)

    assert first["changed"] is True
    assert second["changed"] is False
    assert second["reason"] == "issue relationships unchanged"
    assert len(await _links(db_session)) == 1
    assert len(workflow["comments"]) == 1, "no second PR-linked comment on the board"


@pytest.mark.asyncio
async def test_an_edit_that_keeps_the_same_issue_is_a_no_op(db_session, workflow) -> None:
    """PR opened WITH `Fixes #94`, then edited for wording. Nothing should move."""
    await _seed_issue_task(db_session, 94, TASK_94)
    await ph.reconcile_pr_issue_links(_pr("Fixes #94"), db_session)
    workflow["comments"].clear()

    res = await ph.reconcile_pr_issue_links(
        _pr("Fixes #94\n\nNow with a longer explanation."), db_session
    )

    assert res["changed"] is False
    assert len(await _links(db_session)) == 1
    assert workflow["comments"] == []


@pytest.mark.asyncio
async def test_moving_the_reference_relinks_and_withdraws_the_old_link(
    db_session, workflow
) -> None:
    """`Fixes #94` edited to `Fixes #95`: the new link is canonical, the old is withdrawn."""
    await _seed_issue_task(db_session, 94, TASK_94)
    await _seed_issue_task(db_session, 95, TASK_95)
    await ph.reconcile_pr_issue_links(_pr("Fixes #94"), db_session)

    res = await ph.reconcile_pr_issue_links(_pr("Fixes #95"), db_session)

    assert res["linked"] == [{"issue": 95, "task_id": TASK_95}]
    assert res["withdrawn"] == [{"issue": 94, "task_id": TASK_94}]
    links = {row.github_issue_number: row for row in await _links(db_session)}
    assert links[95].withdrawn_at is None, "the new relationship is live"
    assert links[94].withdrawn_at is not None, "the old one is withdrawn, not deleted"
    assert links[94].plaky_task_id == TASK_94, "the old mapping is preserved for history"


@pytest.mark.asyncio
async def test_a_reference_with_no_plaky_task_never_tears_down_a_working_link(
    db_session, workflow
) -> None:
    """Requirement: never destroy a valid mapping unless the new one is authoritative."""
    await _seed_issue_task(db_session, 94, TASK_94)
    await ph.reconcile_pr_issue_links(_pr("Fixes #94"), db_session)

    # #777 has no IssueTaskMap, so there is nothing authoritative to move to.
    res = await ph.reconcile_pr_issue_links(_pr("Fixes #777"), db_session)

    assert res["linked"] == []
    assert res["withdrawn"] == []
    assert res["unresolved_issues"] == [777]
    links = {row.github_issue_number: row for row in await _links(db_session)}
    assert links[94].withdrawn_at is None, "the working link survives"


@pytest.mark.asyncio
async def test_removing_every_reference_leaves_the_link_alone(db_session, workflow) -> None:
    await _seed_issue_task(db_session, 94, TASK_94)
    await ph.reconcile_pr_issue_links(_pr("Fixes #94"), db_session)

    res = await ph.reconcile_pr_issue_links(_pr("Just a description now."), db_session)

    assert res["changed"] is False
    assert res["reason"] == "no explicit issue reference"
    links = {row.github_issue_number: row for row in await _links(db_session)}
    assert links[94].withdrawn_at is None


@pytest.mark.asyncio
async def test_an_explicit_issue_url_links_the_same_as_a_hash_reference(
    db_session, workflow
) -> None:
    """GitHub's Development panel writes a URL, not `#94`."""
    await _seed_issue_task(db_session, 94, TASK_94)

    res = await ph.reconcile_pr_issue_links(
        _pr(f"Fixes https://github.com/{FULL}/issues/94"), db_session
    )

    assert res["linked"] == [{"issue": 94, "task_id": TASK_94}]


@pytest.mark.asyncio
async def test_a_past_tense_keyword_links_too(db_session, workflow) -> None:
    """GitHub accepts closed/fixed/resolved; so must we."""
    await _seed_issue_task(db_session, 94, TASK_94)

    res = await ph.reconcile_pr_issue_links(_pr("Resolved #94"), db_session)

    assert res["linked"] == [{"issue": 94, "task_id": TASK_94}]


@pytest.mark.asyncio
async def test_a_reference_in_the_title_links(db_session, workflow) -> None:
    await _seed_issue_task(db_session, 94, TASK_94)

    res = await ph.reconcile_pr_issue_links(
        _pr("no reference in the body", title="Add retries (Fixes #94)"), db_session
    )

    assert res["linked"] == [{"issue": 94, "task_id": TASK_94}]


@pytest.mark.asyncio
async def test_a_branch_name_links_only_when_nothing_explicit_was_written(
    db_session, workflow
) -> None:
    await _seed_issue_task(db_session, 94, TASK_94)
    await _seed_issue_task(db_session, 95, TASK_95)

    res = await ph.reconcile_pr_issue_links(
        _pr("no keywords here", head_ref="issue-94-add-retries"), db_session
    )
    assert res["linked"] == [{"issue": 94, "task_id": TASK_94}]


@pytest.mark.asyncio
async def test_a_written_keyword_beats_the_branch_name(db_session, workflow) -> None:
    """A branch naming issue 94 must not add it to a PR whose body says it fixes 95."""
    await _seed_issue_task(db_session, 94, TASK_94)
    await _seed_issue_task(db_session, 95, TASK_95)

    res = await ph.reconcile_pr_issue_links(
        _pr("Fixes #95", head_ref="issue-94-add-retries"), db_session
    )

    assert res["linked"] == [{"issue": 95, "task_id": TASK_95}]
    assert [row.github_issue_number for row in await _links(db_session)] == [95]


@pytest.mark.asyncio
async def test_a_closed_pr_edit_is_still_ignored(db_session, workflow) -> None:
    """Unchanged behaviour: edits to a closed PR do not touch the board."""
    await _seed_issue_task(db_session, 94, TASK_94)
    payload = _pr("Fixes #94")
    payload.pull_request.state = "closed"

    res = await ph.handle_pr_edited(payload, db_session)

    assert res.get("skipped") is True
    assert await _links(db_session) == []


@pytest.mark.asyncio
async def test_handle_pr_edited_runs_the_reconciliation(db_session, workflow, monkeypatch) -> None:
    """The end-to-end path, not just the helper: `edited` must reach the relink."""
    await _seed_issue_task(db_session, 94, TASK_94)

    async def one_task(session: Any, *, github_repo: str, github_pr_number: int) -> list[str]:
        return [TASK_94]

    monkeypatch.setattr(ph, "distinct_task_ids_for_pr", one_task)

    res = await ph.handle_pr_edited(_pr("Fixes #94"), db_session)

    assert res["relink"]["linked"] == [{"issue": 94, "task_id": TASK_94}]
    assert len(await _links(db_session)) == 1


@pytest.mark.asyncio
async def test_the_relink_is_recorded_in_the_sync_log(db_session, workflow) -> None:
    await _seed_issue_task(db_session, 94, TASK_94)
    await _seed_issue_task(db_session, 95, TASK_95)
    await ph.reconcile_pr_issue_links(_pr("Fixes #94"), db_session)
    await ph.reconcile_pr_issue_links(_pr("Fixes #95"), db_session)

    rows = list((await db_session.execute(select(SyncLog))).scalars())
    actions = {row.action for row in rows}
    assert "pr_linked" in actions
    assert "pr_link_withdrawn" in actions


@pytest.mark.asyncio
async def test_a_standalone_pr_task_is_superseded_when_the_issue_link_arrives(
    db_session, workflow
) -> None:
    """The live shape of the bug: PR #88 had its own task (issue_number=0) because nobody
    knew which issue it belonged to, while issue #94 already owned 7209283."""
    await _seed_issue_task(db_session, 94, TASK_94)
    db_session.add(
        PullRequestTaskLink(
            github_repo=REPO,
            github_pr_number=88,
            github_issue_number=0,
            plaky_task_id="7183844",
            link_source="pr_task_created",
        )
    )
    await db_session.commit()

    res = await ph.reconcile_pr_issue_links(_pr("Fixes #94"), db_session)

    assert res["linked"] == [{"issue": 94, "task_id": TASK_94}]
    assert res["withdrawn"] == [{"issue": 0, "task_id": "7183844"}]
    links = {row.github_issue_number: row for row in await _links(db_session)}
    assert links[94].withdrawn_at is None, "the issue's task is now canonical"
    assert links[0].withdrawn_at is not None, "one PR must not drive two cards"
    assert links[0].plaky_task_id == "7183844", "the old task is retired, never deleted"


@pytest.mark.asyncio
async def test_a_standalone_task_survives_when_no_issue_task_exists(db_session, workflow) -> None:
    """Nothing authoritative to move to means nothing is torn down."""
    db_session.add(
        PullRequestTaskLink(
            github_repo=REPO,
            github_pr_number=88,
            github_issue_number=0,
            plaky_task_id="7183844",
            link_source="pr_task_created",
        )
    )
    await db_session.commit()

    res = await ph.reconcile_pr_issue_links(_pr("Fixes #94"), db_session)

    assert res["linked"] == []
    assert res["withdrawn"] == []
    links = {row.github_issue_number: row for row in await _links(db_session)}
    assert links[0].withdrawn_at is None


@pytest.mark.asyncio
async def test_a_branch_only_reference_leaves_an_existing_link_completely_alone(
    db_session, workflow
) -> None:
    """A branch name may establish the FIRST relationship. It may not add a second card
    beside one that already exists, and it may not retire that one either -- both
    outcomes end with one PR driving two cards or the wrong one."""
    await _seed_issue_task(db_session, 94, TASK_94)
    db_session.add(
        PullRequestTaskLink(
            github_repo=REPO,
            github_pr_number=88,
            github_issue_number=0,
            plaky_task_id="7183844",
            link_source="pr_task_created",
        )
    )
    await db_session.commit()

    res = await ph.reconcile_pr_issue_links(
        _pr("no keywords here", head_ref="issue-94-add-retries"), db_session
    )

    assert res["changed"] is False
    assert "branch-only" in res["reason"]
    links = {row.github_issue_number: row for row in await _links(db_session)}
    assert set(links) == {0}, "no second link was created"
    assert links[0].withdrawn_at is None, "and the existing one was not torn down"


@pytest.mark.parametrize(
    "text",
    ["hotfix #123", "postfix #55", "unfixed #7", "affix #9", "preclose #3", "refs #12"],
)
def test_a_word_that_merely_ends_in_a_keyword_is_not_a_reference(text: str) -> None:
    """ "hotfix #123" is a description, not a relationship. Without a word boundary it
    linked the PR to issue 123 -- and a stray number can now retire the correct link."""
    from boardman.services.issue_handler import explicit_issue_numbers

    assert explicit_issue_numbers(text) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Fixes #94", [94]),
        ("fix #94", [94]),
        ("Fixed #94", [94]),
        ("Closes #94", [94]),
        ("Closed #94", [94]),
        ("Resolves #94", [94]),
        ("Resolved #94", [94]),
        ("see the hotfix, but this Fixes #94", [94]),
    ],
)
def test_every_tense_github_accepts_still_links(text: str, expected: list[int]) -> None:
    from boardman.services.issue_handler import explicit_issue_numbers

    assert explicit_issue_numbers(text) == expected


@pytest.mark.asyncio
async def test_a_retired_link_stops_receiving_writes(db_session, workflow) -> None:
    """Retiring a link is only real if the resolver every write path uses honours it."""
    from boardman.services.pr_task_registry import distinct_task_ids_for_pr

    await _seed_issue_task(db_session, 94, TASK_94)
    db_session.add(
        PullRequestTaskLink(
            github_repo=REPO,
            github_pr_number=88,
            github_issue_number=0,
            plaky_task_id="7183844",
            link_source="pr_task_created",
        )
    )
    await db_session.commit()

    before = await distinct_task_ids_for_pr(db_session, github_repo=REPO, github_pr_number=88)
    assert before == ["7183844"]

    await ph.reconcile_pr_issue_links(_pr("Fixes #94"), db_session)

    after = await distinct_task_ids_for_pr(db_session, github_repo=REPO, github_pr_number=88)
    assert after == [TASK_94], "one PR, one card: the retired one is gone from the resolver"


@pytest.mark.asyncio
async def test_a_keyword_in_the_title_links_at_open_not_only_on_a_later_edit(
    db_session, workflow, monkeypatch
) -> None:
    """Reading only the body at open time created a duplicate standalone task for a PR
    whose reference lived in its title."""
    await _seed_issue_task(db_session, 94, TASK_94)
    payload = _pr("no reference in the body", title="Add retries (Fixes #94)")
    payload.action = "opened"

    async def no_pipeline(*_a, **_k):
        raise AssertionError("a titled reference must link, not fall through to triage")

    monkeypatch.setattr(ph, "run_pr_task_pipeline", no_pipeline)
    monkeypatch.setattr(ph, "upsert_pr_row", lambda *_a, **_k: _ok())

    res = await ph.handle_pr_opened(payload, db_session)

    assert any(r.get("issue") == 94 for r in res.get("results", []) or []) or res.get("ok")
    links = {row.github_issue_number: row for row in await _links(db_session)}
    assert 94 in links and links[94].plaky_task_id == TASK_94


async def _ok(*_a, **_k):
    return {"ok": True}


@pytest.mark.parametrize(
    "ref",
    ["feature/2-factor-auth", "release/2024-q1", "94-add-retries", "fix/94-add-retries"],
)
def test_an_ambiguous_branch_number_is_not_read_as_an_issue(ref: str) -> None:
    """`feature/2-factor-auth` is indistinguishable from `94-add-retries`, and reading it
    wrong files a PR's notice, type, assignee, QA assignment and Needs QA onto a
    stranger's task. A branch is a weak signal; the ambiguous half is not worth its cost."""
    from boardman.services.issue_handler import branch_issue_numbers

    assert branch_issue_numbers(ref) == []


@pytest.mark.parametrize("ref", ["issue-94", "issue/94", "gh-94", "gh_94", "feat/issue-94-x"])
def test_an_unambiguous_branch_prefix_still_links(ref: str) -> None:
    from boardman.services.issue_handler import branch_issue_numbers

    assert branch_issue_numbers(ref) == [94]


@pytest.mark.asyncio
async def test_reopening_a_pr_brings_its_links_back(db_session) -> None:
    """Closing without merging withdraws the links, and nothing on the opened path clears
    that once a triage record exists -- so every later event resolved to no task."""
    from boardman.services.pr_task_registry import (
        distinct_task_ids_for_pr,
        mark_pr_withdrawn,
        revive_pr_links,
    )

    db_session.add(
        PullRequestTaskLink(
            github_repo=REPO,
            github_pr_number=88,
            github_issue_number=94,
            plaky_task_id=TASK_94,
            link_source="issue_keyword",
        )
    )
    await db_session.commit()

    await mark_pr_withdrawn(db_session, github_repo=REPO, github_pr_number=88)
    await db_session.commit()
    assert await distinct_task_ids_for_pr(db_session, github_repo=REPO, github_pr_number=88) == []

    await revive_pr_links(db_session, github_repo=REPO, github_pr_number=88)
    await db_session.commit()

    assert await distinct_task_ids_for_pr(db_session, github_repo=REPO, github_pr_number=88) == [
        TASK_94
    ]


@pytest.mark.asyncio
async def test_reopening_does_not_revive_a_superseded_card(db_session, workflow) -> None:
    """Reopening a PR does not un-say which issue it closes."""
    from boardman.services.pr_task_registry import (
        distinct_task_ids_for_pr,
        mark_pr_withdrawn,
        revive_pr_links,
    )

    await _seed_issue_task(db_session, 94, TASK_94)
    db_session.add(
        PullRequestTaskLink(
            github_repo=REPO,
            github_pr_number=88,
            github_issue_number=0,
            plaky_task_id="7183844",
            link_source="pr_task_created",
        )
    )
    await db_session.commit()
    await ph.reconcile_pr_issue_links(_pr("Fixes #94"), db_session)

    await mark_pr_withdrawn(db_session, github_repo=REPO, github_pr_number=88)
    await db_session.commit()
    await revive_pr_links(db_session, github_repo=REPO, github_pr_number=88)
    await db_session.commit()

    live = await distinct_task_ids_for_pr(db_session, github_repo=REPO, github_pr_number=88)
    assert live == [TASK_94], "the issue's task comes back; the superseded card stays retired"


@pytest.mark.asyncio
async def test_a_superseded_card_is_told_it_was_superseded(db_session, workflow) -> None:
    """Retiring the row stops the writes; it does not tell anyone looking at the card."""
    await _seed_issue_task(db_session, 94, TASK_94)
    db_session.add(
        PullRequestTaskLink(
            github_repo=REPO,
            github_pr_number=88,
            github_issue_number=0,
            plaky_task_id="7183844",
            link_source="pr_task_created",
        )
    )
    await db_session.commit()

    await ph.reconcile_pr_issue_links(_pr("Fixes #94"), db_session)

    notices = [body for task_id, body in workflow["comments"] if task_id == "7183844"]
    assert notices, "the superseded card gets a comment"
    assert "Superseded" in notices[0]
    assert TASK_94 in notices[0], "and it names where the work moved to"


@pytest.mark.asyncio
async def test_a_task_is_never_told_its_work_moved_to_itself(db_session, workflow) -> None:
    """The ambiguous-triage path writes BOTH a standalone link and an IssueTaskMap for the
    same task when a PR names an issue that had no task yet. Sweeping standalone rows
    without checking would retire that task and rewind it out of the QA queue."""
    same = "7183844"
    await _seed_issue_task(db_session, 94, same)
    db_session.add(
        PullRequestTaskLink(
            github_repo=REPO,
            github_pr_number=88,
            github_issue_number=0,
            plaky_task_id=same,
            link_source="pr_task_created",
        )
    )
    await db_session.commit()

    res = await ph.reconcile_pr_issue_links(_pr("Fixes #94"), db_session)

    assert res["linked"] == [{"issue": 94, "task_id": same}]
    assert res["withdrawn"] == [], "a task cannot supersede itself"
    notices = [body for task_id, body in workflow["comments"] if "Superseded" in body]
    assert notices == [], "and it is not told its work moved to itself"

    from boardman.services.pr_task_registry import distinct_task_ids_for_pr

    assert await distinct_task_ids_for_pr(db_session, github_repo=REPO, github_pr_number=88) == [
        same
    ], "the PR still drives exactly that one card"


@pytest.mark.asyncio
async def test_repointing_a_pr_does_not_rewind_the_old_issues_card(db_session, workflow) -> None:
    """#94's card was not created for this PR, other PRs may still be open on it, and QA
    may be part way through. Unlink it; do not comment on it or move it."""
    await _seed_issue_task(db_session, 94, TASK_94)
    await _seed_issue_task(db_session, 95, TASK_95)
    await ph.reconcile_pr_issue_links(_pr("Fixes #94"), db_session)
    workflow["comments"].clear()

    res = await ph.reconcile_pr_issue_links(_pr("Fixes #95"), db_session)

    assert res["withdrawn"] == [{"issue": 94, "task_id": TASK_94}]
    notices = [body for task_id, body in workflow["comments"] if task_id == TASK_94]
    assert notices == [], "no 'created for the PR' note on a card that was not"


@pytest.mark.asyncio
async def test_reopening_does_not_resurrect_a_repointed_link(db_session, workflow) -> None:
    """A link retired because the PR names a different issue must not come back either."""
    from boardman.services.pr_task_registry import (
        distinct_task_ids_for_pr,
        mark_pr_withdrawn,
        revive_pr_links,
    )

    await _seed_issue_task(db_session, 94, TASK_94)
    await _seed_issue_task(db_session, 95, TASK_95)
    await ph.reconcile_pr_issue_links(_pr("Fixes #94"), db_session)
    await ph.reconcile_pr_issue_links(_pr("Fixes #95"), db_session)

    await mark_pr_withdrawn(db_session, github_repo=REPO, github_pr_number=88)
    await db_session.commit()
    await revive_pr_links(db_session, github_repo=REPO, github_pr_number=88)
    await db_session.commit()

    assert await distinct_task_ids_for_pr(db_session, github_repo=REPO, github_pr_number=88) == [
        TASK_95
    ], "one card, the one the PR actually names"


@pytest.mark.asyncio
async def test_the_needs_qa_guard_works_with_a_configured_status_value(monkeypatch) -> None:
    """The guard read the field key only when the VALUE came from the board, so every
    deployment that sets PLAKY_STATUS_NEEDS_QA silently lost it."""
    from boardman.settings import settings

    monkeypatch.setattr(settings, "plaky_status_needs_qa", "needs-qa-literal")
    monkeypatch.setattr(settings, "plaky_pr_needs_qa_status", "")

    asked: list[str] = []

    async def fake_resolve(board_id, *, intent):
        return ("status_key", "resolved-id")

    async def fake_current(_board_id, _task_id, field_key):
        asked.append(field_key)
        return "github_pr_review_approved"

    async def fake_status(task_id, *_a, **_k):
        raise AssertionError("a task past QA must not be asked for QA again")

    monkeypatch.setattr("boardman.plaky.dynamic_qa_status.resolve_plaky_status_patch", fake_resolve)
    monkeypatch.setattr("boardman.plaky.dynamic_qa_status.current_status_intent", fake_current)
    monkeypatch.setattr(ph, "_update_plaky_task_status", fake_status)

    await ph._maybe_set_needs_qa(None, TASK_94, False, "269031", allow_regression=False)

    assert asked == ["status_key"], "the field key is resolved even with a configured value"


@pytest.mark.asyncio
async def test_a_newly_opened_pr_still_asks_for_qa_on_a_finished_task(
    db_session, workflow, monkeypatch
) -> None:
    """New work on finished work still needs review, and nothing else on the board would
    say so. Only a LATE link is held back."""
    await _seed_issue_task(db_session, 94, TASK_94)
    payload = _pr("Fixes #94")
    payload.action = "opened"

    async def no_pipeline(*_a, **_k):
        raise AssertionError("an explicit reference must link, not fall through to triage")

    async def ok(*_a, **_k):
        return {"ok": True}

    monkeypatch.setattr(ph, "run_pr_task_pipeline", no_pipeline)
    monkeypatch.setattr(ph, "upsert_pr_row", ok)

    await ph.handle_pr_opened(payload, db_session)

    assert workflow["needs_qa"] == [
        (TASK_94, False, True)
    ], "opened asks for QA unguarded; the guard is for late links only"


@pytest.mark.asyncio
async def test_a_late_link_is_still_held_back(db_session, workflow) -> None:
    await _seed_issue_task(db_session, 94, TASK_94)

    await ph.reconcile_pr_issue_links(_pr("Fixes #94"), db_session)

    assert workflow["needs_qa"] == [(TASK_94, False, False)]


@pytest.mark.asyncio
async def test_reopening_revives_links_from_any_entry_point(
    db_session, workflow, monkeypatch
) -> None:
    """The poller dispatches `reopened` straight to handle_pr_opened, so the revive has to
    live there and not in the HTTP route."""
    from boardman.services.pr_task_registry import (
        distinct_task_ids_for_pr,
        mark_pr_withdrawn,
    )

    await _seed_issue_task(db_session, 94, TASK_94)
    db_session.add(
        PullRequestTaskLink(
            github_repo=REPO,
            github_pr_number=88,
            github_issue_number=94,
            plaky_task_id=TASK_94,
            link_source="issue_keyword",
        )
    )
    await db_session.commit()
    await mark_pr_withdrawn(db_session, github_repo=REPO, github_pr_number=88)
    await db_session.commit()
    assert await distinct_task_ids_for_pr(db_session, github_repo=REPO, github_pr_number=88) == []

    async def ok(*_a, **_k):
        return {"ok": True}

    monkeypatch.setattr(ph, "upsert_pr_row", ok)
    payload = _pr("Fixes #94")
    payload.action = "reopened"

    await ph.handle_pr_opened(payload, db_session)

    assert await distinct_task_ids_for_pr(db_session, github_repo=REPO, github_pr_number=88) == [
        TASK_94
    ]


@pytest.mark.asyncio
async def test_another_repos_issue_url_is_not_read_as_this_repos_issue(
    db_session, workflow
) -> None:
    """The number in another repo's issue URL says nothing about this repo's issue 94, and
    treating it as local runs the whole open pipeline on a stranger's task."""
    await _seed_issue_task(db_session, 94, TASK_94)

    res = await ph.reconcile_pr_issue_links(
        _pr("Fixes https://github.com/Team-Deepiri/deepiri-ui/issues/94"), db_session
    )

    assert res["changed"] is False
    assert res["reason"] == "no explicit issue reference"
    assert await _links(db_session) == []


@pytest.mark.asyncio
async def test_this_repos_issue_url_still_links(db_session, workflow) -> None:
    await _seed_issue_task(db_session, 94, TASK_94)

    res = await ph.reconcile_pr_issue_links(
        _pr(f"Fixes https://github.com/{FULL}/issues/94"), db_session
    )

    assert res["linked"] == [{"issue": 94, "task_id": TASK_94}]


@pytest.mark.asyncio
async def test_merging_does_not_complete_a_superseded_card(db_session, workflow) -> None:
    """A card told "this will not receive further updates" must not then be completed."""
    from boardman.services.pr_task_registry import mark_pr_merged

    await _seed_issue_task(db_session, 94, TASK_94)
    db_session.add(
        PullRequestTaskLink(
            github_repo=REPO,
            github_pr_number=88,
            github_issue_number=0,
            plaky_task_id="7183844",
            link_source="pr_task_created",
        )
    )
    await db_session.commit()
    await ph.reconcile_pr_issue_links(_pr("Fixes #94"), db_session)

    merged = await mark_pr_merged(db_session, github_repo=REPO, github_pr_number=88)

    assert [row.plaky_task_id for row in merged] == [
        TASK_94
    ], "only the card the PR actually drives is completed"


@pytest.mark.asyncio
async def test_every_body_only_caller_also_ignores_another_repos_url(db_session) -> None:
    """The legacy body-only helper feeds merge, ready-for-review, review comments and the
    triage pipeline. Without the repo it read a cross-repo URL as a local issue, and
    handle_pr_merged then completed an unrelated task."""
    from boardman.services.issue_handler import get_linked_issue_numbers
    from boardman.services.pr_task_linking import should_run_pipeline

    other = "Fixes https://github.com/Team-Deepiri/deepiri-ui/issues/94"

    assert await get_linked_issue_numbers(other, repo_full_name=FULL) == []
    assert await get_linked_issue_numbers(
        f"Fixes https://github.com/{FULL}/issues/94", repo_full_name=FULL
    ) == [94]
    # A PR that only cites another repo is unlinked, so triage must still run for it.
    assert await should_run_pipeline(other, repo_full_name=FULL) is True
    assert await should_run_pipeline("Fixes #94", repo_full_name=FULL) is False


@pytest.mark.parametrize(
    "ref", ["feat/gh-2fa-login", "chore/gh-3rd-party-sdk", "deploy/gh-1st", "gh-2fa"]
)
def test_a_digit_that_starts_a_word_is_not_an_issue_number(ref: str) -> None:
    """`gh-2fa-login` is not issue 2. Since opening a PR consults the branch, reading it
    that way runs the whole open pipeline against a stranger's task."""
    from boardman.services.issue_handler import branch_issue_numbers

    assert branch_issue_numbers(ref) == []


@pytest.mark.parametrize("ref", ["gh-94", "issue-94", "gh-123-fix-thing", "issue-94-add-retries"])
def test_a_real_prefixed_issue_number_still_links(ref: str) -> None:
    from boardman.services.issue_handler import branch_issue_numbers

    assert branch_issue_numbers(ref) in ([94], [123])


@pytest.mark.asyncio
async def test_any_event_on_an_open_pr_clears_a_stale_withdrawal(
    db_session, workflow, monkeypatch
) -> None:
    """Keying recovery on `reopened` alone was too narrow: that delivery can be lost, and
    the poller's closed-PR memory does not survive a restart. Either way the PR resolved
    to zero tasks forever once the resolver started honouring the flag."""
    from boardman.services.pr_task_registry import distinct_task_ids_for_pr, mark_pr_withdrawn

    db_session.add(
        PullRequestTaskLink(
            github_repo=REPO,
            github_pr_number=88,
            github_issue_number=0,
            plaky_task_id="7183844",
            link_source="pr_task_created",
        )
    )
    await db_session.commit()
    await mark_pr_withdrawn(db_session, github_repo=REPO, github_pr_number=88)
    await db_session.commit()
    assert await distinct_task_ids_for_pr(db_session, github_repo=REPO, github_pr_number=88) == []

    # An ordinary edit, not a reopen.
    await ph.handle_pr_edited(_pr("no issue reference"), db_session)

    assert await distinct_task_ids_for_pr(db_session, github_repo=REPO, github_pr_number=88) == [
        "7183844"
    ]


@pytest.mark.asyncio
async def test_a_closed_pr_event_does_not_revive_anything(db_session, workflow) -> None:
    """Only seeing the PR OPEN is proof the withdrawal is stale."""
    from boardman.services.pr_task_registry import distinct_task_ids_for_pr, mark_pr_withdrawn

    db_session.add(
        PullRequestTaskLink(
            github_repo=REPO,
            github_pr_number=88,
            github_issue_number=0,
            plaky_task_id="7183844",
            link_source="pr_task_created",
        )
    )
    await db_session.commit()
    await mark_pr_withdrawn(db_session, github_repo=REPO, github_pr_number=88)
    await db_session.commit()

    payload = _pr("no issue reference")
    payload.pull_request.state = "closed"
    await ph._ensure_links_live(payload, db_session)

    assert await distinct_task_ids_for_pr(db_session, github_repo=REPO, github_pr_number=88) == []
