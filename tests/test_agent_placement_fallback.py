"""The assistant interrogated Ali about which repo/board to use for a repo that
repos.yml already routes (live, 2026-08-18: "Which repo is boardman? Which Plaky
board/group?"). Chat now falls back to the same routing the webhook path uses when
the UI sends no board selection. Explicit UI selection still wins.
"""

from __future__ import annotations

import pytest

import boardman.repos_config as rc
from boardman.agent.plaky_prompt_extra import plaky_placement_markdown
from boardman.agent.service import _placement_fallback_from_routing, _resolve_placement

BOTS = rc.RepoRouting(
    category="devtools",
    plaky_table="deepiri-boardman",
    plaky_board_id="269028",
    plaky_group_id="933385",
)


def test_named_repo_uses_its_routing(monkeypatch) -> None:
    monkeypatch.setattr(rc, "get_routing", lambda full, short, org: BOTS)
    bid, gid, note = _placement_fallback_from_routing("deepiri-boardman")
    assert (bid, gid) == ("269028", "933385")
    assert "never ask" in note


def test_single_configured_repo_wins_without_a_repo_hint(monkeypatch) -> None:
    monkeypatch.setattr(rc, "get_routing", lambda full, short, org: None)
    monkeypatch.setattr(
        rc, "list_registered_repos", lambda: {"Team-Deepiri/deepiri-boardman": BOTS}
    )
    bid, gid, note = _placement_fallback_from_routing(None)
    assert (bid, gid) == ("269028", "933385")
    assert "Team-Deepiri/deepiri-boardman" in note


def test_named_but_unrouted_repo_stays_silent(monkeypatch) -> None:
    """Review-confirmed: substituting the single configured repo's board for a chat
    about a DIFFERENT repo files that repo's tasks into deepiri-boardman's group and
    names the wrong repo as the subject. A named repo without routing gets nothing."""
    monkeypatch.setattr(rc, "get_routing", lambda full, short, org: None)
    monkeypatch.setattr(
        rc, "list_registered_repos", lambda: {"Team-Deepiri/deepiri-boardman": BOTS}
    )
    assert _placement_fallback_from_routing("Team-Deepiri/deepiri-core") == (None, None, "")


def test_explicit_board_selection_bypasses_the_fallback(monkeypatch) -> None:
    def boom(repo):
        raise AssertionError("fallback must not run when the UI selected a board")

    monkeypatch.setattr("boardman.agent.service._placement_fallback_from_routing", boom)
    assert _resolve_placement("999", "888", "any-repo") == ("999", "888", "")


def test_silence_resolves_through_the_fallback(monkeypatch) -> None:
    monkeypatch.setattr(
        "boardman.agent.service._placement_fallback_from_routing",
        lambda repo: ("269028", "933385", "Routing note."),
    )
    assert _resolve_placement(None, None, None) == ("269028", "933385", "Routing note.")
    # A caller-supplied group is kept even when the board comes from routing.
    assert _resolve_placement("", "777", None) == ("269028", "777", "Routing note.")


def test_no_routing_answer_leaves_the_request_untouched(monkeypatch) -> None:
    monkeypatch.setattr(
        "boardman.agent.service._placement_fallback_from_routing",
        lambda repo: (None, None, ""),
    )
    assert _resolve_placement(None, "777", None) == (None, "777", "")


def test_many_configured_repos_stay_silent(monkeypatch) -> None:
    """With several routed repos and no hint, guessing a board would file tasks on the
    wrong project; the model keeps its existing lookup tools instead."""
    monkeypatch.setattr(rc, "get_routing", lambda full, short, org: None)
    monkeypatch.setattr(
        rc,
        "list_registered_repos",
        lambda: {
            "Team-Deepiri/a": BOTS,
            "Team-Deepiri/b": rc.RepoRouting(plaky_board_id="111", plaky_group_id="222"),
        },
    )
    assert _placement_fallback_from_routing(None) == (None, None, "")


def test_routing_errors_never_break_chat(monkeypatch) -> None:
    def boom(*a, **k):
        raise RuntimeError("yaml on fire")

    monkeypatch.setattr(rc, "get_routing", boom)
    monkeypatch.setattr(rc, "list_registered_repos", boom)
    assert _placement_fallback_from_routing("x") == (None, None, "")


def test_note_lands_in_the_placement_markdown() -> None:
    md = plaky_placement_markdown("269028", "933385", "Routing note: from repos.yml.")
    assert "Routing note: from repos.yml." in md
    assert "`269028`" in md and "`933385`" in md


def test_markdown_without_note_is_unchanged() -> None:
    md = plaky_placement_markdown("269028", "933385")
    assert "Routing note" not in md


# --- the org roster: Boardman must know what repos exist ------------------------------


@pytest.mark.asyncio
async def test_roster_names_every_repo_and_forbids_claiming_absence(monkeypatch) -> None:
    """Asked "what is aarflingo", Boardman said it was not a Deepiri project while
    deepiri-aarflingo sat in the org. It cannot report absence from a partial view."""
    import boardman.agent.org_roster as orm

    async def fake_fetch(client, org, *, skip_archived=True):
        return ["Team-Deepiri/deepiri-aarflingo", "Team-Deepiri/diri-cyrex"]

    monkeypatch.setattr("boardman.github.org_repos.fetch_org_repository_full_names", fake_fetch)
    monkeypatch.setattr(orm.settings, "github_org", "Team-Deepiri")
    monkeypatch.setattr(orm.settings, "github_pat", "t")

    out = await orm.org_repo_roster_markdown()
    assert "deepiri-aarflingo" in out and "diri-cyrex" in out
    assert "Never say a project does not exist" in out


@pytest.mark.asyncio
async def test_roster_is_silent_when_it_cannot_be_fetched(monkeypatch) -> None:
    """A missing roster must not become a claim about what exists."""
    import boardman.agent.org_roster as orm

    async def boom(client, org, *, skip_archived=True):
        raise RuntimeError("github down")

    monkeypatch.setattr("boardman.github.org_repos.fetch_org_repository_full_names", boom)
    monkeypatch.setattr(orm.settings, "github_org", "Team-Deepiri")
    monkeypatch.setattr(orm.settings, "github_pat", "t")
    assert await orm.org_repo_roster_markdown() == ""


@pytest.mark.asyncio
async def test_roster_needs_a_token(monkeypatch) -> None:
    import boardman.agent.org_roster as orm

    monkeypatch.setattr(orm.settings, "github_pat", "")
    assert await orm.org_repo_roster_markdown() == ""
