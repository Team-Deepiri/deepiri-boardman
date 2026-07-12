from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from boardman.plaky.client import PlakyClient
from boardman.planning.huddle.team_plaky_boards import (
    PlakyBoardRef,
    board_for_team,
    boards_for_team,
    load_team_plaky_boards,
)
from boardman.settings import settings

log = logging.getLogger(__name__)


@dataclass(slots=True)
class PlakyItemSummary:
    item_id: str
    title: str
    status: str
    assignees: str
    updated_at: str
    board_label: str


class PlakyPlanningContext:
    def __init__(self) -> None:
        self._team_boards = load_team_plaky_boards()
        self._highlight_statuses = {
            s.strip().lower()
            for s in settings.planning_plaky_highlight_statuses.split(",")
            if s.strip()
        }

    def enabled(self) -> bool:
        return bool(settings.plaky_api_key)

    def fetch_recent_items(self, team_focus: str) -> list[PlakyItemSummary]:
        if not self.enabled():
            return []
        boards = boards_for_team(self._team_boards, team_focus)
        if not boards:
            single = board_for_team(self._team_boards, team_focus)
            boards = [single] if single else []
        if not boards:
            log.info("planning_plaky_no_boards team_focus=%r", team_focus)
            return []
        cutoff = datetime.now(UTC) - timedelta(days=settings.planning_plaky_lookback_days)
        summaries: list[PlakyItemSummary] = []
        for board in boards:
            label = f"board={board.board_id}"
            try:
                raw_items = asyncio.run(self._list_items(board))
            except Exception as exc:
                log.warning(
                    "planning_plaky_board_failed board=%s error_type=%s error=%s",
                    label,
                    type(exc).__name__,
                    str(exc)[:400],
                )
                continue
            for item in raw_items:
                updated = _item_updated_at(item)
                if updated is not None and updated < cutoff:
                    continue
                summaries.append(_to_summary(item, board_label=label))
        summaries.sort(key=lambda row: row.updated_at, reverse=True)
        return summaries

    def context_markdown(self, team_focus: str) -> str:
        if not self.enabled():
            return "Plaky not configured (set PLAKY_API_KEY)."
        items = self.fetch_recent_items(team_focus)
        lookback = settings.planning_plaky_lookback_days
        if not items:
            boards = boards_for_team(self._team_boards, team_focus)
            if not boards:
                return (
                    "No Plaky board mapped for this team. "
                    f"Edit {settings.planning_team_plaky_boards_file}."
                )
            return f"No Plaky items updated in the last {lookback} days."
        return self._format_markdown(items, team_focus, lookback)

    async def _list_items(self, board: PlakyBoardRef) -> list[dict[str, Any]]:
        client = PlakyClient()
        result = await client.list_board_items(board.board_id, max_pages=15)
        if not result.get("ok"):
            msg = str(result.get("message") or "list_board_items failed")
            raise RuntimeError(msg)
        rows = result.get("items") or []
        return [x for x in rows if isinstance(x, dict)]

    def _format_markdown(
        self,
        items: list[PlakyItemSummary],
        team_focus: str,
        lookback_days: int,
    ) -> str:
        by_status: dict[str, list[PlakyItemSummary]] = {}
        for item in items:
            by_status.setdefault(item.status, []).append(item)

        highlight_keys = [
            status for status in by_status if status.lower() in self._highlight_statuses
        ]
        other_keys = sorted(status for status in by_status if status not in highlight_keys)
        ordered_statuses = sorted(highlight_keys, key=str.lower) + other_keys

        lines = [
            f"## Plaky Board Items (last {lookback_days} days)",
            f"- Team focus: {team_focus}",
            f"- Items with recent activity: {len(items)}",
            "",
        ]
        for status in ordered_statuses:
            rows = by_status[status]
            lines.append(f"### {status} ({len(rows)})")
            for row in rows[:20]:
                assignees = row.assignees or "unassigned"
                updated = row.updated_at[:10] if row.updated_at else "unknown"
                lines.append(
                    f"- {row.title} — assignees: {assignees} — "
                    f"updated: {updated} — board: {row.board_label}"
                )
            if len(rows) > 20:
                lines.append(f"- … and {len(rows) - 20} more")
            lines.append("")
        return "\n".join(lines).strip()


def _to_summary(item: dict[str, Any], board_label: str) -> PlakyItemSummary:
    title = str(
        item.get("title") or item.get("name") or item.get("summary") or "Untitled item"
    ).strip()
    item_id = str(item.get("id") or item.get("itemId") or item.get("uuid") or "unknown")
    return PlakyItemSummary(
        item_id=item_id,
        title=title,
        status=_item_status(item),
        assignees=_item_assignees(item),
        updated_at=str(
            item.get("updatedAt")
            or item.get("updated_at")
            or item.get("modifiedAt")
            or item.get("lastModified")
            or ""
        ),
        board_label=board_label,
    )


def _item_status(item: dict[str, Any]) -> str:
    direct = item.get("status")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    if isinstance(direct, dict):
        for key in ("label", "name", "value", "title"):
            value = direct.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    fields = item.get("fields")
    if isinstance(fields, dict):
        for key, value in fields.items():
            if "status" in str(key).lower():
                parsed = _field_value_label(value)
                if parsed:
                    return parsed
    if isinstance(fields, list):
        for field in fields:
            if not isinstance(field, dict):
                continue
            key = str(field.get("key") or field.get("name") or field.get("id") or "")
            field_type = str(field.get("type") or field.get("fieldType") or "").lower()
            if "status" in key.lower() or field_type == "status":
                parsed = _field_value_label(field.get("value") or field)
                if parsed:
                    return parsed
    group = item.get("group") or item.get("itemGroup")
    if isinstance(group, dict):
        for key in ("name", "title", "label"):
            value = group.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return "Unknown"


def _item_assignees(item: dict[str, Any]) -> str:
    fields = item.get("fields")
    names: list[str] = []
    if isinstance(fields, dict):
        for key, value in fields.items():
            if any(token in str(key).lower() for token in ("person", "assignee", "owner")):
                label = _field_value_label(value)
                if label:
                    names.append(label)
    if isinstance(fields, list):
        for field in fields:
            if not isinstance(field, dict):
                continue
            key = str(field.get("key") or field.get("name") or "")
            if any(token in key.lower() for token in ("person", "assignee", "owner")):
                label = _field_value_label(field.get("value") or field)
                if label:
                    names.append(label)
    direct = item.get("assignees") or item.get("owners")
    if isinstance(direct, list):
        for entry in direct:
            if isinstance(entry, dict):
                for key in ("name", "label", "email", "displayName"):
                    value = entry.get(key)
                    if isinstance(value, str) and value.strip():
                        names.append(value.strip())
            elif isinstance(entry, str) and entry.strip():
                names.append(entry.strip())
    return ", ".join(_unique_preserve(names))


def _field_value_label(value: object) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        for key in ("label", "name", "title", "value", "displayValue"):
            nested = value.get(key)
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
        members = value.get("members") or value.get("users") or value.get("people")
        if isinstance(members, list):
            labels = []
            for member in members:
                if isinstance(member, dict):
                    for key in ("name", "label", "email"):
                        text = member.get(key)
                        if isinstance(text, str) and text.strip():
                            labels.append(text.strip())
                            break
                elif isinstance(member, str) and member.strip():
                    labels.append(member.strip())
            return ", ".join(_unique_preserve(labels))
    if isinstance(value, list):
        labels = [_field_value_label(entry) for entry in value]
        return ", ".join(label for label in labels if label)
    return ""


def _item_updated_at(item: dict[str, Any]) -> datetime | None:
    for key in ("updatedAt", "updated_at", "modifiedAt", "lastModified", "createdAt"):
        parsed = _parse_datetime(item.get(key))
        if parsed is not None:
            return parsed
    return None


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _unique_preserve(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered
