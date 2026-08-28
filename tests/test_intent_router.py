"""The deterministic router: questions whose whole answer is already in memory.

Two failure modes, and the second is much worse than the first. Missing an intent costs
one LLM call. Claiming an intent wrongly produces an instant, confident, wrong answer with
no tool call to correct it — so every case here that should fall through is as important
as every case that should be answered.
"""

from __future__ import annotations

import pytest

from boardman.agent.brain import Briefing, Identity, LiveState, ProjectState, TrackedPR
from boardman.agent.fast_path import maybe_fast_path

REPO = "Team-Deepiri/deepiri-boardman"


def _state(
    *,
    branch: str = "main",
    issues: list[int] | None = None,
    prs: list[TrackedPR] | None = None,
    merged: int = 0,
    available: bool = True,
) -> ProjectState:
    return ProjectState(
        identity=Identity(
            repo_full_name=REPO,
            repo_short="deepiri-boardman",
            board_id="269028",
            group_id="933385",
            table="deepiri-boardman",
            default_branch=branch,
        ),
        briefing=Briefing(payload={"ok": True}, state="fresh"),
        live=LiveState(
            tracked_issues=issues if issues is not None else [92, 91, 90],
            active_prs=prs if prs is not None else [],
            # Derived exactly as _load_live derives it: distinct PR numbers, not link rows.
            open_pr_count=len({p.number for p in (prs or [])}),
            merged_prs=merged,
            available=available,
        ),
    )


async def _ask(text: str, state: ProjectState | None = None):
    return await maybe_fast_path(
        text, repo=REPO, board_id="269028", group_id="933385", state=state or _state()
    )


# --- answered from state, no LLM, no network -------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question",
    [
        "what is the default branch?",
        "what's the default branch",
        "which is the default branch",
        "the default branch is what exactly",
    ],
)
async def test_default_branch_is_answered_from_state(question: str) -> None:
    result = await _ask(question)
    assert result is not None and result.intent == "default_branch"
    assert "main" in result.reply


@pytest.mark.asyncio
async def test_no_known_branch_falls_through_rather_than_guessing() -> None:
    assert await _ask("what is the default branch?", _state(branch="")) is None


@pytest.mark.asyncio
async def test_issue_count_comes_from_the_mapping_table() -> None:
    result = await _ask("how many issues are on the board?")
    assert result is not None and result.intent == "issue_count"
    assert "3 issues" in result.reply
    assert "#92" in result.reply


@pytest.mark.asyncio
async def test_zero_issues_is_said_plainly() -> None:
    result = await _ask("how many issues do we have tracked", _state(issues=[]))
    assert result is not None
    assert "no issues" in result.reply.casefold()


@pytest.mark.asyncio
async def test_pr_count_lists_the_links_it_knows() -> None:
    prs = [TrackedPR(number=88, task_id="t1", issue_number=90, link_source="issue_keyword")]
    result = await _ask("how many pull requests are linked?", _state(prs=prs, merged=12))
    assert result is not None and result.intent == "pr_count"
    assert "PR #88" in result.reply and "issue #90" in result.reply
    assert "12 have merged" in result.reply


@pytest.mark.asyncio
async def test_issue_to_task_lookup() -> None:
    result = await _ask("which plaky task is issue 91?")
    assert result is not None and result.intent == "issue_task_lookup"
    assert "#91" in result.reply


@pytest.mark.asyncio
async def test_an_unmapped_issue_is_reported_as_unmapped() -> None:
    """The mapping table IS the source of truth for this one, so absence is an answer."""
    result = await _ask("is there a task for issue 4242?")
    assert result is not None
    assert "no plaky task" in result.reply.casefold()


@pytest.mark.asyncio
async def test_an_issue_with_an_open_pr_says_so() -> None:
    prs = [TrackedPR(number=88, task_id="t90", issue_number=90, link_source="issue_keyword")]
    result = await _ask("which task is issue 90 linked to?", _state(prs=prs))
    assert result is not None
    assert "PR #88" in result.reply and "t90" in result.reply


# --- must NOT be answered from state ---------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question",
    [
        "how many issues are open on github right now?",
        "how many PRs are open currently",
        "check github right now and tell me the issue count",
        "what is the latest issue count",
    ],
)
async def test_explicitly_live_questions_are_not_answered_from_cache(question: str) -> None:
    """ "Right now" is a request to go and look. Answering it from a mapping table is
    exactly the stale-answer failure the spec calls out."""
    assert await _ask(question) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question",
    [
        "create a task for issue 90",
        "close the task for issue 91",
        "assign issue 90 to someone",
        "update the default branch",
        "delete the task for issue 92",
    ],
)
async def test_a_write_request_never_takes_a_read_only_shortcut(question: str) -> None:
    assert await _ask(question) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question",
    [
        "why is issue 90 taking so long?",
        "what should we work on next?",
        "is PR 88 safe to merge?",
        "summarise what happened this week",
        "how does the QA picker work?",
    ],
)
async def test_judgment_questions_go_to_the_agent(question: str) -> None:
    assert await _ask(question) is None


@pytest.mark.asyncio
async def test_no_state_means_no_shortcut() -> None:
    assert (
        await maybe_fast_path(
            "how many issues are on the board?",
            repo=REPO,
            board_id="269028",
            group_id="933385",
            state=None,
        )
        is None
    )


@pytest.mark.asyncio
async def test_unreadable_live_state_falls_through() -> None:
    """If the L2 tables could not be read, silence is correct; a zero is a lie."""
    assert await _ask("how many issues are tracked?", _state(available=False)) is None


@pytest.mark.asyncio
async def test_a_state_without_a_repo_is_ignored() -> None:
    empty = ProjectState(identity=Identity(), briefing=Briefing(), live=LiveState())
    assert await _ask("what is the default branch?", empty) is None


@pytest.mark.asyncio
async def test_the_router_makes_no_network_call(monkeypatch) -> None:
    import httpx

    def explode(*_a, **_k):
        raise AssertionError("the router must answer from memory")

    monkeypatch.setattr(httpx.AsyncClient, "send", explode)
    monkeypatch.setattr(httpx.Client, "send", explode)
    for q in ("what is the default branch?", "how many issues are on the board?"):
        assert await _ask(q) is not None


# --- the loose matches an adversarial review found ---------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question",
    [
        "what branch should I cut this fix from?",
        "why did the main branch CI go red?",
        "which branch has the QA changes?",
        "should this branch be rebased?",
    ],
)
async def test_branch_questions_that_are_not_about_the_default_branch(question: str) -> None:
    """The old pattern answered every one of these with a one-line branch name and ended
    the turn."""
    result = await _ask(question)
    assert result is None or result.intent != "default_branch"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question",
    [
        "how many open issues are there?",
        "how many issues are still open",
        "how many closed issues do we have",
        "how many PRs are open",
        "how many merged pull requests",
    ],
)
async def test_open_and_closed_counts_are_github_questions(question: str) -> None:
    """issue_task_map keeps a row for every issue ever synced, closed ones included, so
    it can answer "how many are on the board" and never "how many are open"."""
    assert await _ask(question) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question",
    [
        "what is the default branch of deepiri-sorge?",
        "how many issues does Team-Deepiri/diva have on the board?",
        "which task is issue 5 in diri-cyrex",
    ],
)
async def test_a_question_about_another_repo_is_never_answered_from_this_repos_state(
    question: str,
) -> None:
    """The session repo only switches for slugs in repos.yml, so these arrive carrying
    boardman's state. Answering from it is a confident answer about the wrong project."""
    assert await _ask(question) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question",
    [
        "which task is 3 days old?",
        "is there a task with 5 subtasks?",
        "which card has 2 reviewers",
    ],
)
async def test_a_number_near_the_word_task_is_not_an_issue_number(question: str) -> None:
    """ "which task is 3 days old" resolved 3 as an issue number and asserted it unmapped."""
    result = await _ask(question)
    assert result is None or result.intent != "issue_task_lookup"


@pytest.mark.asyncio
async def test_a_pr_closing_several_issues_is_counted_once() -> None:
    """pr_task_links holds one row per (PR, issue). Counting rows reported one PR as three."""
    prs = [
        TrackedPR(number=88, task_id="t", issue_number=10, link_source="issue_keyword"),
        TrackedPR(number=88, task_id="t", issue_number=11, link_source="issue_keyword"),
        TrackedPR(number=88, task_id="t", issue_number=12, link_source="issue_keyword"),
    ]
    state = ProjectState(
        identity=_state().identity,
        briefing=Briefing(payload={"ok": True}, state="fresh"),
        live=LiveState(active_prs=prs, open_pr_count=1, merged_prs=0, available=True),
    )
    result = await _ask("how many pull requests are linked?", state)
    assert result is not None
    assert result.reply.startswith("1 pull request "), result.reply
    assert result.reply.count("PR #88") == 1
    assert "#10, #11, #12" in result.reply
