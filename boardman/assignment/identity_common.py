"""Shared GitHub / Plaky profile text extraction (no LLM, no scoring)."""

from __future__ import annotations

import re
import unicodedata
from typing import Any


def normalize_identity_text(s: str) -> str:
    if not s:
        return ""
    t = unicodedata.normalize("NFKC", s)
    t = t.replace("<", " ").replace(">", " ")
    return " ".join(t.split()).strip()


def extract_email_from_angle(s: str) -> str:
    raw = (s or "").strip()
    m = re.search(r"<([^>\s]+@[^>\s]+)>", raw, re.I)
    if m:
        return m.group(1).strip().lower()
    t = normalize_identity_text(raw)
    return t.casefold().strip()


def github_public_email(gh: dict[str, Any]) -> str:
    v = gh.get("email")
    if isinstance(v, str) and v.strip():
        return extract_email_from_angle(v)
    return ""


def plaky_email_addresses(p: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for k in ("email", "primaryEmail", "mail", "userEmail"):
        v = p.get(k)
        if isinstance(v, str) and v.strip():
            out.append(extract_email_from_angle(v))
    raw_list = p.get("emails")
    if isinstance(raw_list, list):
        for item in raw_list:
            if isinstance(item, str) and item.strip():
                out.append(extract_email_from_angle(item))
            elif isinstance(item, dict):
                ev = item.get("email") or item.get("value")
                if isinstance(ev, str) and ev.strip():
                    out.append(extract_email_from_angle(ev))
    seen: set[str] = set()
    uniq: list[str] = []
    for e in out:
        if e and e not in seen:
            seen.add(e)
            uniq.append(e)
    return uniq


def plaky_display_name(p: dict[str, Any]) -> str:
    return normalize_identity_text(
        str(p.get("name") or p.get("displayName") or p.get("fullName") or "")
    )
