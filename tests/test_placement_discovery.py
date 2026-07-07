"""Unit tests for Plaky repo -> board/group placement discovery."""

from __future__ import annotations

import pytest

from boardman.plaky.placement_discovery import discover_placement_from_catalog
from boardman.plaky.plaky_catalog import PlakyBoardEntry, PlakyCatalogCache, PlakyGroupEntry
from boardman.plaky.repo_category import (
    PLAKY_BOARD_BOTS,
    PLAKY_BOARD_DEV_TOOLS,
    PLAKY_BOARD_PLATFORM,
    infer_repo_category,
    plaky_board_query_for_category,
)


def _catalog() -> PlakyCatalogCache:
    boards = [
        PlakyBoardEntry(
            id="b-platform",
            name=PLAKY_BOARD_PLATFORM,
            groups=[
                PlakyGroupEntry(id="g-synapse", name="deepiri-synapse"),
                PlakyGroupEntry(id="g-api", name="deepiri-api-gateway"),
            ],
        ),
        PlakyBoardEntry(
            id="b-bots",
            name=PLAKY_BOARD_BOTS,
            groups=[
                PlakyGroupEntry(id="g-boardman", name="deepiri-boardman"),
                PlakyGroupEntry(id="g-cyrex", name="diri-cyrex"),
            ],
        ),
        PlakyBoardEntry(
            id="b-dx",
            name=PLAKY_BOARD_DEV_TOOLS,
            groups=[
                PlakyGroupEntry(id="g-sorge", name="deepiri-sorge"),
            ],
        ),
    ]
    return PlakyCatalogCache(fetched_at=1.0, source="test", boards=boards)


def test_infer_repo_category_boardman_is_dx():
    assert infer_repo_category("boardman") == "dx"
    assert plaky_board_query_for_category("dx") == PLAKY_BOARD_DEV_TOOLS


def test_infer_repo_category_cyrex_is_ai_runtime():
    assert infer_repo_category("cyrex") == "ai-runtime"
    assert plaky_board_query_for_category("ai-runtime") == PLAKY_BOARD_BOTS


def test_discover_placement_group_slug_match():
    cat = _catalog()
    result = discover_placement_from_catalog(
        cat, "Team-Deepiri/deepiri-boardman", "deepiri-boardman"
    )
    assert result is not None
    assert result.source == "group_slug_match"
    assert result.board_id == "b-bots"
    assert result.group_id == "g-boardman"
    assert result.group_name == "deepiri-boardman"


def test_discover_placement_group_slug_match_on_dx_board():
    cat = _catalog()
    result = discover_placement_from_catalog(cat, "Team-Deepiri/deepiri-sorge", "deepiri-sorge")
    assert result is not None
    assert result.board_id == "b-dx"
    assert result.group_id == "g-sorge"
    assert result.source == "group_slug_match"


def test_discover_placement_matches_prefixed_group_name():
    cat = _catalog()
    result = discover_placement_from_catalog(cat, "Team-Deepiri/synapse", "deepiri-synapse")
    assert result is not None
    assert result.source == "group_slug_match"
    assert result.category == "platform"
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
        name=PLAKY_BOARD_BOTS,
        groups=[PlakyGroupEntry(id="g-boardman", name="deepiri-boardman")],
    )
    cat = PlakyCatalogCache(fetched_at=1.0, source="test", boards=[legacy, bots])
    result = discover_placement_from_catalog(
        cat, "Team-Deepiri/deepiri-boardman", "deepiri-boardman"
    )
    assert result is not None
    assert result.board_id == "b-bots"
    assert result.group_id == "g-boardman"


def test_filter_categorical_boards():
    from boardman.plaky.plaky_catalog import filter_categorical_boards

    boards = [
        PlakyBoardEntry(id="1", name="AI Task Board", groups=[]),
        PlakyBoardEntry(id="2", name=PLAKY_BOARD_BOTS, groups=[]),
    ]
    filtered = filter_categorical_boards(boards)
    assert len(filtered) == 1
    assert filtered[0].name == PLAKY_BOARD_BOTS


@pytest.mark.asyncio
async def test_get_routing_async_uses_discovery(monkeypatch):
    from boardman.plaky.placement_discovery import PlacementResult
    from boardman.repos_config import get_routing_async

    async def fake_resolve(*_a, **_k):
        return PlacementResult(
            board_id="b1",
            group_id="g1",
            board_name=PLAKY_BOARD_BOTS,
            group_name="boardman",
            category="dx",
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
