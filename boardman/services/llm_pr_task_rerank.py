"""
Optional LLM: rerank a fixed list of Plaky task candidates for a PR (gray zone only).

Must return one of the provided task_ids or null — no free-form invention.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence

from boardman.llm.factory import get_chat_model
from boardman.llm.ollama_autodetect import effective_ollama_model
from boardman.settings import settings

_log = logging.getLogger(__name__)


def _chat_model():
    p = (settings.llm_provider or "ollama").lower()
    if p == "ollama":
        from langchain_ollama import ChatOllama

        return ChatOllama(
            model=effective_ollama_model(None),
            base_url=settings.ollama_base_url.rstrip("/"),
            temperature=0,
        )
    return get_chat_model()


def _build_prompt(
    *,
    repo_full: str,
    pr_title: str,
    pr_body: str,
    candidates: Sequence[tuple[str, str, str]],
) -> str:
    lines = [
        "You match one GitHub pull request to at most one Plaky task from the CANDIDATES list.",
        "Pick the task the PR most likely implements or belongs to. "
        "If none fit, return null for task_id.",
        "",
        f"Repository: {repo_full}",
        f"PR title: {pr_title}",
        f"PR body (may be truncated):\n{(pr_body or '')[:6000]}",
        "",
        "CANDIDATES (task_id — title — description excerpt):",
    ]
    for tid, title, desc in candidates:
        lines.append(f"- {tid} — {title[:200]} — {(desc or '')[:400]}")
    lines.extend(
        [
            "",
            "Reply with ONE JSON object only, no markdown fences:",
            '{"task_id": "<one of the ids above or null>", '
            '"confidence": 0.0-1.0, "reason": "short"}',
        ]
    )
    return "\n".join(lines)


def _parse_rerank(
    text: str, allowed_ids: set[str]
) -> tuple[str | None, float, str]:
    if not text or not text.strip():
        return None, 0.0, ""
    raw = text.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
    raw = re.sub(r"\s*```\s*$", "", raw)
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None, 0.0, ""
    try:
        obj = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None, 0.0, ""
    if not isinstance(obj, dict):
        return None, 0.0, ""
    tid = obj.get("task_id")
    if tid is not None and tid != "null":
        tid = str(tid).strip()
        if tid not in allowed_ids:
            return None, 0.0, "invalid task_id"
    else:
        tid = None
    try:
        conf = float(obj.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    conf = max(0.0, min(1.0, conf))
    reason = str(obj.get("reason") or "")[:500]
    return tid, conf, reason


def llm_rerank_pr_candidates(
    *,
    repo_full: str,
    pr_title: str,
    pr_body: str,
    candidates: Sequence[tuple[str, str, str]],
) -> tuple[str | None, float, str]:
    """
    Returns (chosen_task_id_or_none, confidence 0..1, reason).
    """
    if not settings.pr_linking_llm_enabled or not candidates:
        return None, 0.0, ""
    allowed = {c[0] for c in candidates if c[0]}
    if not allowed:
        return None, 0.0, ""
    prompt = _build_prompt(
        repo_full=repo_full,
        pr_title=pr_title,
        pr_body=pr_body or "",
        candidates=candidates,
    )
    try:
        from langchain_core.messages import HumanMessage

        model = _chat_model()
        msg = model.invoke([HumanMessage(content=prompt)])
        content = getattr(msg, "content", None) or str(msg)
        if isinstance(content, list):
            content = "".join(
                getattr(b, "text", str(b)) if not isinstance(b, str) else b for b in content
            )
        tid, conf, reason = _parse_rerank(str(content), allowed)
        return tid, conf, reason
    except Exception as e:
        _log.warning("PR-task rerank LLM failed: %s", e)
        return None, 0.0, str(e)


def clear_pr_link_llm_cache() -> None:
    """Reserved for future caching; no-op for now."""
    return
