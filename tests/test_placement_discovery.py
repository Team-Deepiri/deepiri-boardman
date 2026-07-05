"""Unit tests for Plaky repo -> board/group placement discovery."""

from __future__ import annotations

import pytest

from boardman.plaky.plaky_catalog import (
    PlakyBoardEntry,
    PlakyCatalogCache,
    PlakyGroupEntry,
    filter_categorical_boards,
    is_categorical_board,
    looks_like_repo_group,
)
from boardman.plaky.placement_discovery import discover_placement_from_catalog


def _catalog() -> PlakyCatalogCache:
    boards = [
        PlakyBoardEntry(
            id="b-platform",
            name="Deepiri Platform + Services",
            groups=[
                PlakyGroupEntry(id="g-synapse", name="deepiri-synapse"),
                PlakyGroupEntry(id="g-api", name="deepiri-api-gateway"),
            ],
        ),
        PlakyBoardEntry(
            id="b-bots",
            name="Bots",
            groups=[
                PlakyGroupEntry(id="g-boardman", name="deepiri-boardman"),
                PlakyGroupEntry(id="g-cyrex", name="diri-cyrex"),
            ],
        ),
        PlakyBoardEntry(
            id="b-dx",
            name="Developer Tool Repos",
            groups=[
                PlakyGroupEntry(id="g-sorge", name="deepiri-sorge"),
                PlakyGroupEntry(id="g-shared", name="deepiri-shared-utils"),
            ],
        ),
    ]
    return PlakyCatalogCache(fetched_at=1.0, source="test", boards=boards)


def test_looks_like_repo_group():
    assert looks_like_repo_group("deepiri-boardman")
    assert looks_like_repo_group("diri-cyrex")
    assert looks_like_repo_group("‼️deepiri-platform")
    assert looks_like_repo_group("diva")
    assert not looks_like_repo_group("Backlog")
    assert not looks_like_repo_group("Open PRs")
    assert not looks_like_repo_group("")


def test_is_categorical_board_detects_repo_catalog_boards():
    board = PlakyBoardEntry(
        id="b1",
        name="Any Board Title",
        groups=[
            PlakyGroupEntry(id="g1", name="deepiri-foo"),
            PlakyGroupEntry(id="g2", name="deepiri-bar"),
        ],
    )
    assert is_categorical_board(board)


def test_is_categorical_board_rejects_sprint_board():
    board = PlakyBoardEntry(
        id="b-legacy",
        name="AI Task Board",
        groups=[
            PlakyGroupEntry(id="g1", name="Backlog"),
            PlakyGroupEntry(id="g2", name="Open PRs"),
            PlakyGroupEntry(id="g3", name="In Progress"),
        ],
    )
    assert not is_categorical_board(board)


def test_is_categorical_board_rejects_single_group_test_board():
    board = PlakyBoardEntry(
        id="b-test",
        name="Boardman Test Board",
        groups=[PlakyGroupEntry(id="g-test", name="Boardman")],
    )
    assert not is_categorical_board(board)


def test_discover_placement_group_slug_match():
    cat = _catalog()
    result = discover_placement_from_catalog(cat, "Team-Deepiri/deepiri-boardman", "deepiri-boardman")
    assert result is not None
    assert result.source == "group_slug_match"
    assert result.board_id == "b-bots"
    assert result.group_id == "g-boardman"
    assert result.group_name == "deepiri-boardman"
    assert result.category == "Bots"


def test_discover_placement_group_slug_match_on_dx_board():
    cat = _catalog()
    result = discover_placement_from_catalog(cat, "Team-Deepiri/deepiri-sorge", "deepiri-sorge")
    assert result is not None
    assert result.board_id == "b-dx"
    assert result.group_id == "g-sorge"
    assert result.source == "group_slug_match"
    assert result.category == "Developer Tool Repos"


def test_discover_placement_matches_prefixed_group_name():
    cat = _catalog()
    result = discover_placement_from_catalog(cat, "Team-Deepiri/synapse", "deepiri-synapse")
    assert result is not None
    assert result.source == "group_slug_match"
    assert result.category == "Deepiri Platform + Services"
    assert result.board_id == "b-platform"
    assert result.group_id == "g-synapse"


def test_discover_placement_no_group_returns_none():
    cat = _catalog()
    result = discover_placement_from_catalog(cat, "Team-Deepiri/brand-new-repo", "brand-new-repo")
    assert result is None


def test_discover_placement_ignores_legacy_boards():
    legacy = PlakyBoardEntry(
        id="b-legacy",
        name="Boardman Test Board",
        groups=[PlakyGroupEntry(id="g-test", name="Boardman")],
    )
    bots = PlakyBoardEntry(
        id="b-bots",
        name="Bots",
        groups=[PlakyGroupEntry(id="g-boardman", name="deepiri-boardman")],
    )
    cat = PlakyCatalogCache(fetched_at=1.0, source="test", boards=[legacy, bots])
    result = discover_placement_from_catalog(cat, "Team-Deepiri/deepiri-boardman", "deepiri-boardman")
    assert result is not None
    assert result.board_id == "b-bots"
    assert result.group_id == "g-boardman"


def test_filter_categorical_boards():
    boards = [
        PlakyBoardEntry(
            id="1",
            name="AI Task Board",
            groups=[
                PlakyGroupEntry(id="g1", name="Backlog"),
                PlakyGroupEntry(id="g2", name="Open PRs"),
            ],
        ),
        PlakyBoardEntry(
            id="2",
            name="Bots",
            groups=[
                PlakyGroupEntry(id="g1", name="deepiri-boardman"),
                PlakyGroupEntry(id="g2", name="deepiri-sorge"),
            ],
        ),
    ]
    filtered = filter_categorical_boards(boards)
    assert len(filtered) == 1
    assert filtered[0].name == "Bots"


@pytest.mark.asyncio
async def test_get_routing_async_uses_discovery(monkeypatch):
    from boardman.repos_config import get_routing_async
    from boardman.plaky.placement_discovery import PlacementResult

    async def fake_resolve(*_a, **_k):
        return PlacementResult(
            board_id="b1",
            group_id="g1",
            board_name="Bots",
            group_name="deepiri-boardman",
            category="Bots",
            source="group_slug_match",
            score=500,
        )

    monkeypatch.setattr(
        "boardman.plaky.placement_discovery.resolve_placement_for_repo",
        fake_resolve,
    )
    monkeypatch.setattr("boardman.repos_config.settings.plaky_placement_auto_discover", True)

    routing, source = await get_routing_async(
        "Team-Deepiri/boardman",
        "boardman",
        "Team-Deepiri",
        with_source=True,
    )
    assert routing is not None
    assert routing.plaky_board_id == "b1"
    assert routing.plaky_group_id == "g1"
    assert source == "discovered:group_slug_match"
