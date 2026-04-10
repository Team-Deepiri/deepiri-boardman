"""
Optional LLM pass: same-person judgment for GitHub roster ↔ Plaky user (gray-zone scores).

Uses the configured chat provider; Ollama uses temperature=0 via a dedicated ChatOllama instance.
"""

from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from typing import Any, Dict, Optional

from boardman.assignment.identity_common import (
    github_public_email,
    plaky_display_name,
    plaky_email_addresses,
)
from boardman.llm.factory import get_chat_model
from boardman.llm.ollama_autodetect import effective_ollama_model
from boardman.settings import settings

_log = logging.getLogger(__name__)


def _identity_chat_model():
    p = (settings.llm_provider or "ollama").lower()
    if p == "ollama":
        from langchain_ollama import ChatOllama

        return ChatOllama(
            model=effective_ollama_model(None),
            base_url=settings.ollama_base_url.rstrip("/"),
            temperature=0,
        )
    return get_chat_model()


def _build_prompt(gh: Dict[str, Any], plaky: Dict[str, Any]) -> str:
    login = str(gh.get("login") or "").strip()
    gname = str(gh.get("name") or "").strip()
    gemail = github_public_email(gh)
    pname = plaky_display_name(plaky)
    pemails = plaky_email_addresses(plaky)
    em = ", ".join(pemails) if pemails else "(none)"

    return (
        "You are deduplicating employee identities between GitHub and Plaky (work tools).\n"
        "Decide if BOTH records describe the same real person.\n\n"
        f"GitHub: login={login!r}, profile_name={gname!r}, public_email={gemail or '(none)'!r}\n"
        f"Plaky: display_name={pname!r}, emails={em!r}\n\n"
        "Rules:\n"
        "- Informal vs formal first names (Bob/Robert, Kate/Katherine) may be the same person.\n"
        "- Different work vs personal email is OK if name/login clearly align.\n"
        "- If uncertain or likely two different people, set same_person to false.\n\n"
        "Reply with ONE JSON object only, no markdown fences:\n"
        '{"same_person": true or false, "confidence": a number from 0 to 1}\n'
    )


def _parse_confidence(text: str) -> Optional[float]:
    if not text or not text.strip():
        return None
    raw = text.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
    raw = re.sub(r"\s*```\s*$", "", raw)
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    sp = obj.get("same_person")
    try:
        c = float(obj.get("confidence", 0.5 if sp is None else (0.9 if sp else 0.1)))
    except (TypeError, ValueError):
        return None
    c = max(0.0, min(1.0, c))
    if sp is True:
        return max(c, 0.72)
    if sp is False:
        return min(c, 0.35)
    return c


@lru_cache(maxsize=384)
def _cached_same_person(key: str) -> Optional[float]:
    try:
        payload = json.loads(key)
    except json.JSONDecodeError:
        return None
    gh = payload.get("gh") or {}
    pl = payload.get("pl") or {}
    if not isinstance(gh, dict) or not isinstance(pl, dict):
        return None
    prompt = _build_prompt(gh, pl)
    try:
        from langchain_core.messages import HumanMessage

        model = _identity_chat_model()
        msg = model.invoke([HumanMessage(content=prompt)])
        content = getattr(msg, "content", None) or str(msg)
        if isinstance(content, list):
            content = "".join(
                getattr(b, "text", str(b)) if not isinstance(b, str) else b for b in content
            )
        return _parse_confidence(str(content))
    except Exception as e:
        _log.warning("identity LLM call failed: %s", e)
        return None


def llm_same_person_confidence(gh: Dict[str, Any], plaky: Dict[str, Any]) -> Optional[float]:
    if not settings.assignment_identity_llm_enabled:
        return None
    key = json.dumps(
        {
            "gh": {
                "login": str(gh.get("login") or ""),
                "name": str(gh.get("name") or ""),
                "email": github_public_email(gh),
            },
            "pl": {
                "name": plaky_display_name(plaky),
                "emails": plaky_email_addresses(plaky),
            },
        },
        sort_keys=True,
    )
    return _cached_same_person(key)


def clear_identity_llm_cache() -> None:
    _cached_same_person.cache_clear()
