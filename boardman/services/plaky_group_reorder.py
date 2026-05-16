"""
Best-effort reorder of Plaky board items: incomplete first, completed-like last.

Plaky's public OpenAPI does not document a stable item-reorder endpoint; we try a
small set of likely URLs and bodies, then no-op with a clear message.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from boardman.plaky.client import PlakyClient, _headers, _normalize_id, _request_with_rate_limit_retry
from boardman.settings import settings

_log = logging.getLogger(__name__)


def _item_group_id(item: dict[str, Any]) -> str:
    g = item.get("group")
    if isinstance(g, dict):
        gid_sub = str(g.get("id") or "").strip()
    elif g is None or g == "":
        gid_sub = ""
    else:
        gid_sub = str(g).strip()
    return str(
        item.get("groupId")
        or item.get("group_id")
        or item.get("boardGroupId")
        or item.get("sectionId")
        or gid_sub
        or ""
    ).strip()


def _item_id(item: dict[str, Any]) -> str:
    return _normalize_id(item)


def _status_blob(item: dict[str, Any]) -> str:
    parts: list[str] = []
    st = item.get("status")
    if isinstance(st, str):
        parts.append(st)
    elif isinstance(st, dict):
        parts.append(str(st.get("name") or st.get("title") or st.get("label") or ""))
    col = item.get("column")
    if isinstance(col, dict):
        parts.append(str(col.get("name") or col.get("title") or ""))
    return " ".join(parts).casefold()


def _item_looks_done(item: dict[str, Any], markers: tuple[str, ...]) -> bool:
    blob = _status_blob(item)
    if not blob.strip():
        return False
    return any(m in blob for m in markers if m)


async def reorder_group_completed_last(plaky: PlakyClient, board_id: str, group_id: str) -> dict[str, Any]:
    bid = board_id.strip()
    gid = group_id.strip()
    if not bid or not gid:
        return {"ok": False, "skipped": True, "message": "board_id and group_id required"}

    markers = tuple(
        x.strip().casefold()
        for x in settings.plaky_reorder_done_status_markers.split(",")
        if x.strip()
    )
    if not markers:
        markers = ("done", "complete", "closed", "resolved")

    listed = await plaky.list_board_items(bid, max_pages=settings.pr_linking_board_max_pages)
    if not listed.get("ok"):
        return {"ok": False, "message": listed.get("message") or "list_board_items failed"}

    items = [x for x in listed.get("items") or [] if isinstance(x, dict)]
    group_items = [x for x in items if _item_group_id(x) == gid]
    if len(group_items) < 2:
        return {"ok": True, "skipped": True, "message": "not enough items in group to reorder"}

    todo = [x for x in group_items if not _item_looks_done(x, markers)]
    done = [x for x in group_items if _item_looks_done(x, markers)]
    ordered = todo + done
    ids = [_item_id(x) for x in ordered if _item_id(x)]
    if len(ids) < 2:
        return {"ok": True, "skipped": True, "message": "could not read item ids"}

    root = plaky._public_root()
    if not root:
        return {"ok": False, "skipped": True, "message": "Plaky v1/public base URL required for reorder"}

    sid = await plaky.resolve_space_for_board(bid)
    if not sid:
        return {"ok": False, "message": "could not resolve space for board"}

    base = root.rstrip("/")
    hdr = _headers(plaky.api_key)
    bodies: list[tuple[str, dict[str, Any]]] = [
        (f"{base}/spaces/{sid}/boards/{bid}/groups/{gid}/items/order", {"itemIds": ids}),
        (f"{base}/spaces/{sid}/boards/{bid}/groups/{gid}/items/order", {"orderedItemIds": ids}),
        (f"{base}/spaces/{sid}/boards/{bid}/item-groups/{gid}/items/order", {"itemIds": ids}),
        (f"{base}/spaces/{sid}/boards/{bid}/groups/{gid}/reorder", {"itemIds": ids}),
        (f"{base}/spaces/{sid}/boards/{bid}/items/reorder", {"groupId": gid, "itemIds": ids}),
    ]

    async with httpx.AsyncClient() as client:
        for url, body in bodies:
            r = await _request_with_rate_limit_retry(client, "PATCH", url, headers=hdr, json=body)
            if r.status_code in (200, 201, 204):
                return {"ok": True, "url": url, "status": r.status_code, "count": len(ids)}
            if r.status_code not in (404, 405, 400):
                _log.info("plaky reorder unexpected status %s %s", r.status_code, url)

    return {
        "ok": False,
        "skipped": True,
        "message": "No supported reorder endpoint responded OK (Plaky may require manual sort).",
        "tried": len(bodies),
    }
