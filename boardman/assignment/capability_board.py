"""Live-measured QA hardware capability, read from a Plaky board instead of a
hand-typed `tier` in team_assignments.yml.

Schema-driven like every other Plaky read in this codebase (`board_schema.py`): field
KEYS are resolved by matching the configured field NAME against the board's live
schema, never hardcoded ids. If `settings.plaky_capability_board_id` is unset, every
function here degrades to "no data" so callers fall back to team_assignments.yml
unchanged — this is additive, not a replacement that can go dark.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from boardman.plaky.board_schema import fetch_board_schema_bundle
from boardman.plaky.client import PlakyClient
from boardman.settings import settings

_log = logging.getLogger(__name__)

_VALID_TIERS = {"light", "minimal", "low", "standard", "heavy"}


def _normalize_field_name(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.strip().lower())


def _resolve_field_key(fields: list[dict[str, Any]], wanted_name: str) -> str:
    wanted = _normalize_field_name(wanted_name)
    if not wanted:
        return ""
    for f in fields:
        name = _normalize_field_name(str(f.get("name") or ""))
        if name == wanted:
            return str(f.get("key") or "")
    for f in fields:
        name = _normalize_field_name(str(f.get("name") or ""))
        if wanted in name or name in wanted:
            return str(f.get("key") or "")
    return ""


def _option_label(field: dict[str, Any], raw_value: Any) -> str:
    """A choice-type field stores an option id; resolve it to its label. A plain text
    field's raw value is already the label."""
    options = field.get("options") or []
    if not isinstance(options, list) or not options:
        return str(raw_value or "").strip()
    for opt in options:
        if not isinstance(opt, dict):
            continue
        if str(opt.get("id") or "") == str(raw_value or ""):
            return str(opt.get("name") or "").strip()
    return str(raw_value or "").strip()


async def fetch_capability_tiers(plaky: PlakyClient | None = None) -> dict[str, str]:
    """{github_login (lowercased) -> tier string} from the live capability board.

    Empty dict when the board isn't configured, unreadable, or has no usable rows —
    every caller must treat that the same as "no live data, use the config fallback".
    """
    board_id = (settings.plaky_capability_board_id or "").strip()
    if not board_id:
        return {}

    bundle = await fetch_board_schema_bundle(board_id)
    if not bundle.get("ok") or not bundle.get("normalized"):
        _log.info("capability board %s: schema unavailable, falling back to config", board_id)
        return {}
    fields = bundle["normalized"].get("fields") or []
    login_key = _resolve_field_key(fields, settings.plaky_capability_login_field)
    tier_key = _resolve_field_key(fields, settings.plaky_capability_tier_field)
    if not login_key or not tier_key:
        _log.info(
            "capability board %s: could not resolve login/tier field keys from schema "
            "(looked for %r / %r) — falling back to config",
            board_id,
            settings.plaky_capability_login_field,
            settings.plaky_capability_tier_field,
        )
        return {}
    tier_field = next((f for f in fields if f.get("key") == tier_key), {})

    plaky = plaky or PlakyClient()
    listed = await plaky.list_board_items(board_id, max_pages=5)
    if not listed.get("ok"):
        return {}

    out: dict[str, str] = {}
    for item in listed.get("items") or []:
        if not isinstance(item, dict):
            continue
        raw_fields = item.get("fields")
        if not isinstance(raw_fields, dict):
            continue
        login = str(raw_fields.get(login_key) or "").strip().lower()
        if not login:
            continue
        tier = _option_label(tier_field, raw_fields.get(tier_key)).strip().lower()
        if tier in _VALID_TIERS:
            out[login] = tier
        elif tier:
            _log.warning(
                "capability board %s: item for %r has unrecognized tier %r, ignoring",
                board_id,
                login,
                tier,
            )
    return out


async def resolve_hardware_tier(github_login: str, fallback_tier: str) -> str:
    """Live board value for this login if present and valid, else the config fallback
    (team_assignments.yml's per-member `tier`, itself defaulting to "standard")."""
    login = (github_login or "").strip().lower()
    if not login:
        return fallback_tier
    tiers = await fetch_capability_tiers()
    return tiers.get(login, fallback_tier)


def _option_id_for_label(field: dict[str, Any], label: str) -> str | None:
    options = field.get("options") or []
    if not isinstance(options, list):
        return None
    want = label.strip().lower()
    for opt in options:
        if isinstance(opt, dict) and str(opt.get("name") or "").strip().lower() == want:
            return str(opt.get("id") or "") or None
    return None


async def report_hardware_capability(
    *,
    github_login: str,
    tier: str,
    cores: int | None = None,
    ram_gb: float | None = None,
    has_gpu: bool | None = None,
    plaky: PlakyClient | None = None,
) -> dict[str, Any]:
    """Write (or create) this person's row on the capability board with a
    live-measured tier. `cores`/`ram_gb`/`has_gpu` are written opportunistically if a
    field with a matching name exists on the board — never required, since the board's
    columns are whatever a human set up, not something this code assumes."""
    board_id = (settings.plaky_capability_board_id or "").strip()
    if not board_id:
        return {
            "ok": False,
            "message": "PLAKY_CAPABILITY_BOARD_ID not configured — nothing to write to",
        }
    login = (github_login or "").strip()
    if not login:
        return {"ok": False, "message": "github_login is required"}
    if tier.strip().lower() not in _VALID_TIERS:
        return {"ok": False, "message": f"tier {tier!r} is not one of {sorted(_VALID_TIERS)}"}

    bundle = await fetch_board_schema_bundle(board_id)
    if not bundle.get("ok") or not bundle.get("normalized"):
        return {"ok": False, "message": "could not load capability board schema"}
    fields = bundle["normalized"].get("fields") or []
    login_key = _resolve_field_key(fields, settings.plaky_capability_login_field)
    tier_key = _resolve_field_key(fields, settings.plaky_capability_tier_field)
    if not login_key or not tier_key:
        return {
            "ok": False,
            "message": (
                f"could not resolve login/tier field keys on the capability board "
                f"(looked for {settings.plaky_capability_login_field!r} / "
                f"{settings.plaky_capability_tier_field!r})"
            ),
        }
    tier_field = next((f for f in fields if f.get("key") == tier_key), {})
    tier_value: Any = _option_id_for_label(tier_field, tier) or tier

    field_values: dict[str, Any] = {login_key: login, tier_key: tier_value}
    for name, value in (("cores", cores), ("ram_gb", ram_gb), ("gpu", has_gpu)):
        if value is None:
            continue
        key = _resolve_field_key(fields, name)
        if key:
            field_values[key] = value

    plaky = plaky or PlakyClient()
    group_id = (settings.plaky_capability_group_id or "").strip() or None

    listed = await plaky.list_board_items(board_id, max_pages=5)
    existing_id = ""
    if listed.get("ok"):
        for item in listed.get("items") or []:
            if not isinstance(item, dict):
                continue
            raw = item.get("fields")
            if isinstance(raw, dict) and str(raw.get(login_key) or "").strip() == login:
                existing_id = str(item.get("id") or item.get("itemId") or "")
                break

    if existing_id:
        patched = await plaky.patch_item_field_values(board_id, existing_id, field_values)
        patched["item_id"] = existing_id
        patched["action"] = "updated"
        return patched

    created = await plaky.create_task(
        title=login,
        description="Auto-created by `boardman capability-report`.",
        board_id=board_id,
        group_id=group_id,
        field_values=field_values,
    )
    created["action"] = "created"
    return created
