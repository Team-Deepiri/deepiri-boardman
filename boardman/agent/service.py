"""Agent chat with DB-backed session history + optional LangChain tools."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from boardman.agent.brain import (
    get_project_state,
    render_project_state,
    schedule_revalidation,
)
from boardman.agent.fast_path import maybe_fast_path
from boardman.agent.guardrails import has_confirm_token, looks_like_board_organize_request
from boardman.agent.memory_store import db_messages_to_langchain
from boardman.agent.org_roster import org_repo_roster_markdown
from boardman.agent.plaky_prompt_extra import plaky_placement_markdown
from boardman.agent.prompts import BOARD_MANAGER_SYSTEM, TASK_CREATION_WORKFLOW, TEAM_TASK_POLICY
from boardman.agent.repo_resolution import resolve_repo
from boardman.agent.runner import iter_tool_agent, run_tool_agent
from boardman.agent.task_draft import format_task_draft_for_prompt, load_task_draft
from boardman.agent.tool_context import agent_tool_context
from boardman.agent.write_failures import recent_failed_task_writes
from boardman.database.models import AgentMessage, AgentSession
from boardman.llm.completion import chat_complete, chat_complete_stream
from boardman.observability.degradation import log_degraded, log_unexpected
from boardman.plaky.board_schema import fetch_board_schema_bundle
from boardman.settings import settings

logger = logging.getLogger(__name__)

_TOOL_CALLS_JSON_MAX_BYTES = 64000


def _runtime_error_trace(exc: BaseException) -> str:
    return json.dumps(
        [{"tool_name": "agent_runtime", "status": "error", "result_summary": str(exc)[:500]}],
        default=str,
    )


def _serialize_tool_calls_json(traces: list[dict[str, Any]]) -> str | None:
    """Serialize tool traces; drop oldest rows until JSON fits ``_TOOL_CALLS_JSON_MAX_BYTES``."""
    if not traces:
        return None
    rows = list(traces)
    while rows:
        s = json.dumps(rows, default=str)
        if len(s) <= _TOOL_CALLS_JSON_MAX_BYTES:
            return s
        rows.pop(0)
    return json.dumps(traces[-1:], default=str)


ErrorCategory = Literal[
    "model_missing",
    "auth",
    "rate_limited",
    "insufficient_quota",
    "timeout",
    "connectivity",
    "bad_request",
    "upstream_http",
    "unknown",
]


def _normalize_provider(provider: str | None) -> str:
    p = (provider or settings.llm_provider or "ollama").strip().lower()
    aliases = {
        "gpt": "openai",
        "google": "gemini",
        "claude": "anthropic",
        "or": "openrouter",
    }
    return aliases.get(p, p)


def _default_model_for_provider(provider: str) -> str:
    if provider == "anthropic":
        return "claude-sonnet-4-20250514"
    if provider == "openai":
        return "gpt-4.1"
    if provider == "openrouter":
        return "anthropic/claude-3.5-sonnet"
    if provider == "gemini":
        return "gemini-2.0-flash"
    if provider == "ollama":
        explicit = (settings.llm_model or "").strip()
        if explicit:
            return explicit
        # Imported OUTSIDE the call's try: an `except NoOllamaModelAvailable` naming a
        # symbol the try body binds raises NameError while handling, skipping the fallback
        # below. The import gets its own guard so it still cannot break a cosmetic label.
        try:
            from boardman.llm.ollama_autodetect import (
                NoOllamaModelAvailable,
                effective_ollama_model,
            )
        except Exception:  # noqa: BLE001 - the model label is never worth a failed turn
            log_degraded(logger, "_default_model_for_provider: importing ollama_autodetect")
            return "auto-selected from Ollama"

        try:
            return effective_ollama_model(None)
        except NoOllamaModelAvailable:
            # Runs on every turn with the shipped defaults, so it is a fact, not a trace.
            logger.debug("no Ollama model pulled yet; showing a generic model label")
            return "auto-selected from Ollama"
        except Exception:  # noqa: BLE001 — Ollama may not be reachable; the label is cosmetic
            log_degraded(logger, "_default_model_for_provider: effective_ollama_model")
            return "auto-selected from Ollama"
    return (settings.llm_model or "").strip() or "unspecified"


def _resolve_llm_context(
    provider: str | None, model: str | None, *, use_tools: bool
) -> tuple[str, str]:
    prov = _normalize_provider(provider)
    mdl = (
        (model or "").strip()
        or (settings.llm_model or "").strip()
        or _default_model_for_provider(prov)
    )
    return prov, mdl


# OpenAI and Anthropic both answer 429 for two unrelated problems: a per-minute rate
# limit (transient, re-send works) and an empty credit balance (billing; re-sending is
# useless). Telling a user with no credits to "just re-send" is stating something false
# with confidence, so the balance case must be split out by the error body.
_QUOTA_EXHAUSTED_MARKERS = (
    "insufficient_quota",
    "credit_balance_exhausted",
    "no credits remaining",
    "credit balance",
    "insufficient credits",
    "requires more credits",
    "billing hard limit",
    "billing details",
)


def _looks_quota_exhausted(text: str, provider: str = "") -> bool:
    low = text.lower()
    markers = _QUOTA_EXHAUSTED_MARKERS
    if provider in ("gemini", "google"):
        # Gemini reuses OpenAI's "check your plan and billing details" sentence for
        # TRANSIENT per-minute throttles (RetryInfo says retry in seconds), so on
        # Gemini that phrase proves nothing about the balance.
        markers = tuple(m for m in markers if m != "billing details")
    return any(m in low for m in markers)


def _classify_llm_error(exc: BaseException, *, provider: str) -> ErrorCategory:
    try:
        import httpx

        if isinstance(exc, httpx.HTTPStatusError):
            st = exc.response.status_code
            low = ((exc.response.text or "") + " " + str(exc)).lower()
            if st == 404 and (provider != "ollama" or ("model" in low or "/api/chat" in low)):
                return "model_missing"
            if st in (401, 403):
                return "auth"
            if st == 429:
                return (
                    "insufficient_quota"
                    if _looks_quota_exhausted(low, provider)
                    else "rate_limited"
                )
            if st == 402:
                # Payment Required is unambiguous (OpenRouter's drained-balance signal).
                return "insufficient_quota"
            if st == 400:
                # Anthropic reports an empty balance as 400 invalid_request, not 429.
                return (
                    "insufficient_quota" if _looks_quota_exhausted(low, provider) else "bad_request"
                )
            return "upstream_http"
        if isinstance(exc, httpx.ReadTimeout | httpx.TimeoutException):
            return "timeout"
        if isinstance(exc, httpx.ConnectError | OSError):
            return "connectivity"
    except Exception:  # noqa: BLE001 - failure is silenced for resilience
        log_degraded(logger, "_classify_llm_error: inspecting the provider error")

    mod = (getattr(type(exc), "__module__", "") or "").lower()
    if type(exc).__name__ == "ResponseError" and "ollama" in mod:
        low = str(exc).lower()
        if "not found" in low or "model" in low:
            return "model_missing"
        return "upstream_http"

    # Provider SDK exceptions (openai.RateLimitError etc.) are not httpx errors, so the
    # transcript's TPM 429 fell through to "unknown" and the user was told to check
    # their API key — a wrong fix for a full quota. Use the SDK's own status_code when
    # present; NEVER match bare digit substrings ("requested 142900 tokens" contains
    # "429" and would claim a context-length error is a rate limit).
    st = getattr(exc, "status_code", None)
    if st is None:
        st = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(st, int):
        if st == 429:
            return (
                "insufficient_quota"
                if _looks_quota_exhausted(str(exc), provider)
                else "rate_limited"
            )
        if st == 402:
            return "insufficient_quota"
        if st in (401, 403):
            return "auth"
        if st == 404:
            return "model_missing"
        if st == 400:
            return (
                "insufficient_quota"
                if _looks_quota_exhausted(str(exc), provider)
                else "bad_request"
            )

    low = str(exc).lower()
    if "not found" in low and "model" in low:
        return "model_missing"
    if _looks_quota_exhausted(low, provider):
        return "insufficient_quota"
    if "rate limit" in low or "rate_limit_exceeded" in low or "tokens per min" in low:
        return "rate_limited"
    if "invalid api key" in low or "incorrect api key" in low:
        return "auth"
    return "unknown"


def _provider_hint(provider: str, category: ErrorCategory, model: str) -> str:
    if category == "insufficient_quota":
        if provider == "openai":
            return (
                "The OpenAI account has no credits remaining. This is a billing problem, "
                "not a rate limit - re-sending will not help. Add credits at "
                "https://platform.openai.com/settings/organization/billing/ and then "
                "re-send the message."
            )
        if provider == "openrouter":
            return (
                "The OpenRouter account is out of credits. This is a billing problem, "
                "not a rate limit - re-sending will not help. Add credits at "
                "https://openrouter.ai/settings/credits and then re-send the message."
            )
        return (
            "The provider account has run out of credit. This is a billing problem, not "
            "a rate limit - re-sending will not help until the balance is topped up."
        )
    if provider == "ollama":
        if category == "model_missing":
            return (
                "Run `docker compose exec ollama ollama list` (or `ollama list`) and pull a valid tag, "
                f"for example: `docker compose exec ollama ollama pull {model}`. "
                "If **LLM_MODEL** is unset, Boardman auto-selects from `/api/tags`."
            )
        if category == "connectivity":
            return (
                f"Cannot reach Ollama at **{settings.ollama_base_url}**. "
                "If using Docker, set **OLLAMA_BASE_URL=http://ollama:11434** in container env."
            )
        if category == "timeout":
            return (
                "Ollama timed out. Try a smaller model, keep model warm, or raise "
                "**OLLAMA_READ_TIMEOUT_SECONDS**."
            )
        return "Check **OLLAMA_BASE_URL** and confirm Ollama is running."

    if provider == "openrouter":
        if category == "auth":
            return "Verify **OPENROUTER_API_KEY** is set and valid."
        if category == "model_missing":
            return (
                "Use provider-prefixed model IDs (for example `anthropic/claude-3.5-sonnet`) and verify "
                "the model is available for your OpenRouter account."
            )
        if category == "rate_limited":
            return "OpenRouter rate-limited the request. Retry later or switch to another available model."
        return "Check **OPENROUTER_API_KEY**, **OPENROUTER_BASE_URL**, and model availability."

    if provider == "openai":
        if category == "auth":
            return "Verify **OPENAI_API_KEY** and account permissions."
        if category == "model_missing":
            return "Confirm the OpenAI model ID is valid and enabled for your account."
        if category == "rate_limited":
            return (
                "OpenAI rate-limited the request (tokens-per-minute quota). This is NOT an "
                "API-key problem - the quota resets within a minute; just re-send the "
                "message. If it keeps happening, raise the org TPM limit or use a lighter model."
            )
        return (
            "Unexpected error (full text above). This is usually transient - re-send the "
            "message once; if it repeats, the error text is the bug report."
        )

    if provider == "anthropic":
        if category == "auth":
            return "Verify **ANTHROPIC_API_KEY** is set and valid."
        if category == "model_missing":
            return "Confirm the Anthropic model ID is valid for your account."
        if category == "rate_limited":
            return "Anthropic rate-limited the request. Retry later."
        return "Check **ANTHROPIC_API_KEY** and model availability."

    if provider == "gemini":
        if category == "auth":
            return "Verify **GEMINI_API_KEY** is set and valid."
        if category == "model_missing":
            return "Confirm the Gemini model name is valid and available for your project."
        if category == "rate_limited":
            return "Gemini rate-limited the request. Retry later."
        return "Check **GEMINI_API_KEY** and model availability."

    return (
        "Unexpected error (full text above). Re-send the message once; if it repeats, "
        "the error text is the bug report."
    )


def _format_llm_failure(exc: BaseException, *, provider: str, model: str) -> str:
    """User-visible provider-aware message for LLM failures."""
    category = _classify_llm_error(exc, provider=provider)
    base = (str(exc) or type(exc).__name__).strip()
    hint = _provider_hint(provider, category, model)
    try:
        import httpx

        if isinstance(exc, httpx.HTTPStatusError):
            st = exc.response.status_code
            snippet = (exc.response.text or "").strip().replace("\n", " ")[:400]
            base = f"HTTP {st} from the model API"
            if snippet:
                base += f": {snippet}"
    except Exception:  # noqa: BLE001 - LLM failure is handled by the caller
        log_degraded(logger, "_format_llm_failure")
    return (
        "I could not get a reply from the language model.\n\n"
        f"**Provider:** `{provider}`\n"
        f"**Model:** `{model}`\n"
        f"**What went wrong:** {base}\n\n"
        f"**Fix:** {hint}"
    )


async def _load_draft_markdown(session: AsyncSession, agent_session_pk: int | None) -> str:
    if not agent_session_pk:
        return ""
    draft = await load_task_draft(session, agent_session_pk)
    return format_task_draft_for_prompt(draft)


_TOOLS_DOWN_CONSTRAINT = """

## TOOLS ARE UNAVAILABLE THIS TURN (degraded mode)

The tool run failed, so you have NO access to GitHub, Plaky, or the local repo right now.
You are working from conversation text only.

- If the question needs repo, board, issue, or PR data, say plainly that you could not reach
  the tools this turn and suggest retrying. Offer what you can answer without them.
- Do NOT answer from memory or general knowledge about any repository, and do NOT describe
  files, issues, statuses, or commits — anything you produce that way is a guess presented as
  fact, which is worse than admitting the outage.
- Never substitute a different repo (for example this service's own repo) for the one asked about.
"""


async def _safe_plain_chat(
    *,
    message: str,
    repo: str | None,
    history_msgs: list[AgentMessage],
    plaky_suffix: str,
    provider: str | None,
    model: str | None,
    resolved_provider: str,
    resolved_model: str,
    extra_system_suffix: str = "",
    tools_unavailable: bool = False,
) -> str:
    if tools_unavailable:
        extra_system_suffix = (extra_system_suffix or "") + _TOOLS_DOWN_CONSTRAINT
    try:
        llm_messages = _build_plain_llm_messages(
            message,
            repo,
            history_msgs,
            plaky_suffix,
            extra_system_suffix=extra_system_suffix,
        )
        return await chat_complete(llm_messages, provider=provider, model=model)
    except Exception as e:  # noqa: BLE001 - logged and handled
        logger.exception("Plain chat (Ollama/direct LLM) failed")
        return _format_llm_failure(e, provider=resolved_provider, model=resolved_model)


def _placement_fallback_from_routing(repo: str | None) -> tuple[str | None, str | None, str]:
    """Board/group for chat when the UI made no selection.

    repos.yml already routes every configured repo to its board and group for the
    webhook path; the assistant asking Ali "which board?" for a repo the config
    routes was a live failure (it interrogated instead of creating). Explicit UI
    selection still wins — this only fills silence.
    """
    try:
        from boardman.repos_config import get_routing, list_registered_repos

        org = (settings.github_org or "").strip()
        routing = None
        matched = ""
        r = (repo or "").strip()
        if r:
            short = r.split("/")[-1]
            full = r if "/" in r else (f"{org}/{short}" if org else short)
            routing = get_routing(full, short, org)
            matched = full
        if (routing is None or not (routing.plaky_board_id or "").strip()) and not r:
            # Only guess when NO repo was named. A named-but-unrouted repo must stay
            # silent: substituting the one configured repo's board here would file that
            # other repo's tasks into deepiri-boardman's group and tell the model the
            # wrong repo is "the subject" (review-confirmed failure).
            with_boards = [
                (name, rt)
                for name, rt in list_registered_repos().items()
                if (rt.plaky_board_id or "").strip()
            ]
            if len(with_boards) == 1:
                matched, routing = with_boards[0]
        if routing is None or not (routing.plaky_board_id or "").strip():
            return None, None, ""
        label = (routing.plaky_table or "").strip()
        note = (
            f"Routing note: these ids come from repos.yml for **{matched}**"
            + (f' (group label: "{label}")' if label else "")
            + ". Treat that repo as the default subject of board requests — never ask "
            "which repo or board. For requests like 'make it production ready', derive "
            "the specifics from the repo itself (docs, open issues, open PRs, existing "
            "board tasks) instead of asking the user to define them."
        )
        return (
            routing.plaky_board_id.strip(),
            (routing.plaky_group_id or "").strip() or None,
            note,
        )
    except Exception:  # noqa: BLE001 — routing is optional; the agent works without placement
        logger.exception("placement fallback from repos.yml failed")
        return None, None, ""


def _resolve_placement(
    plaky_board_id: str | None,
    plaky_group_id: str | None,
    repo: str | None,
    *,
    repo_named_but_unresolved: bool = False,
) -> tuple[str | None, str | None, str]:
    """Explicit UI selection passes through untouched; silence falls back to routing.

    `repo_named_but_unresolved` is the case the fallback must NOT cover. A repo the user
    named that has no repos.yml entry arrives here as `repo=None`, which looks identical
    to "no repo was mentioned" -- so the single-configured-board guess fired and filed
    that other repo's tasks into this one's group, while telling the model the wrong repo
    was the subject. Silence is a question the assistant can ask; a wrong board is not
    something the user can see.
    """
    if (plaky_board_id or "").strip():
        return plaky_board_id, plaky_group_id, ""
    if repo_named_but_unresolved:
        return plaky_board_id, plaky_group_id, ""
    fb_bid, fb_gid, note = _placement_fallback_from_routing(repo)
    if not fb_bid:
        return plaky_board_id, plaky_group_id, ""
    return fb_bid, ((plaky_group_id or "").strip() or fb_gid), note


async def _plaky_system_suffix(
    plaky_board_id: str | None,
    plaky_group_id: str | None,
    note: str = "",
) -> str:
    out = plaky_placement_markdown(plaky_board_id, plaky_group_id, note)
    bid = (plaky_board_id or "").strip()
    if bid:
        try:
            bundle = await fetch_board_schema_bundle(bid)
            out += bundle.get("markdown") or ""
        except Exception as e:  # noqa: BLE001 — the schema is optional context, not a gate
            logger.warning("Could not load Plaky board schema bundle for %s: %s", bid, e)
            log_unexpected(logger, "_plaky_system_suffix: fetch_board_schema_bundle")
            out += (
                f"\n\n## Current Plaky board schema (from API)\n"
                f"**Board id:** `{bid}`\n"
                "Schema could not be loaded right now; continue using known placement and refresh schema when possible.\n"
            )
    return out


def _record_context_size(extra: str) -> None:
    """Size of the per-turn context blocks, so a prompt diet can be shown to have worked.

    Only the variable part: the constant system prompt is a source-code fact, this is the
    part that changes with the repo, the board, and the draft.
    """
    from boardman.observability.counters import observe, set_gauge

    set_gauge("prompt.extra_chars", len(extra or ""))
    observe("prompt.extra_chars", len(extra or ""))


async def run_agent_chat(
    session: AsyncSession,
    *,
    message: str,
    session_id: str | None,
    repo: str | None,
    provider: str | None = None,
    model: str | None = None,
    allow_writes: bool = False,
    use_tools: bool = False,
    plaky_board_id: str | None = None,
    plaky_group_id: str | None = None,
) -> tuple[str, str]:
    """Persist user message, call LLM (tool agent or plain chat), persist assistant reply."""
    sid = session_id or str(uuid.uuid4())

    q = (
        select(AgentSession)
        .where(AgentSession.session_id == sid)
        .options(selectinload(AgentSession.messages))
    )
    res = await session.execute(q)
    ag: AgentSession | None = res.scalar_one_or_none()

    resolution = resolve_repo(
        explicit_repo=repo,
        session_repo=ag.repo if ag is not None else None,
        message=message,
    )
    active_repo = resolution.repo

    if ag is None:
        ag = AgentSession(
            session_id=sid,
            repo=active_repo,
            prompt_version=settings.prompt_version,
            created_at=datetime.utcnow(),
            last_active=datetime.utcnow(),
        )
        session.add(ag)
        await session.flush()
        history_msgs: list[AgentMessage] = []
    else:
        ag.last_active = datetime.utcnow()
        if active_repo and active_repo != ag.repo:
            ag.repo = active_repo
        history_msgs = sorted(ag.messages, key=lambda m: m.id)[-settings.agent_max_history :]

    plaky_board_id, plaky_group_id, placement_note = _resolve_placement(
        plaky_board_id,
        plaky_group_id,
        active_repo,
        repo_named_but_unresolved=resolution.source == "unknown-mentioned",
    )

    # The user's message goes into history BEFORE the LLM phase: a provider failure
    # (the TPM 429 in the live transcript) must not erase what the user said — losing
    # "X is the assignee" and then filing tasks unassigned is worse than the error.
    session.add(AgentMessage(session_pk=ag.id, role="user", content=message))

    # Release the SQLite write lock NOW: the session-row insert/update above would otherwise
    # hold it through the whole (possibly minutes-long) LLM/tool phase, and every concurrent
    # agent turn would die with "database is locked" after the busy timeout.
    await session.commit()
    agent_session_pk = ag.id

    # Answer small read-only intents without paying for an LLM/tool loop.
    # Everything already known about this repo, assembled without a single network
    # call. Built BEFORE the router so the router can answer from it.
    project_state = await get_project_state(
        session,
        active_repo or "",
        board_id=plaky_board_id or "",
        group_id=plaky_group_id or "",
    )

    # The fast path reads Plaky live, and its client re-raises transport errors. Raised
    # from here -- outside the block that turns a failure into an SSE `error` frame -- an
    # outage truncated the stream with no error event at all, and 500'd the non-streaming
    # path. A fast path that cannot answer is not an error; it just is not a fast path.
    try:
        fast = await maybe_fast_path(
            message,
            repo=active_repo,
            board_id=plaky_board_id,
            group_id=plaky_group_id,
            state=project_state,
        )
    except Exception as exc:  # noqa: BLE001 - fall through to the full agent instead
        log_degraded(logger, "agent: fast path", exc)
        fast = None
    if fast is not None:
        session.add(AgentMessage(session_pk=ag.id, role="assistant", content=fast.reply))
        await session.flush()
        logger.info(
            "agent fast path intent=%s repo=%s source=%s",
            fast.intent,
            active_repo or "",
            resolution.source,
        )
        return fast.reply, sid

    # The model gets the answer instead of instructions for finding it.
    repo_context_prompt = render_project_state(project_state)
    from boardman.cognition.rendering import render_cognition_block

    cognition_data = (
        project_state.briefing.payload.get("cognition") if project_state.briefing.present else None
    )
    repo_context_prompt += render_cognition_block(cognition_data)
    # Serve what we have, then repair it behind the reply. Not awaited: this turn is
    # already answered, and queuing the refresh is a DB write the asker must not wait on.
    schedule_revalidation(project_state)
    cache_state = project_state.briefing.state
    if active_repo:
        logger.info(
            "agent repo context=%s source=%s cache=%s",
            active_repo,
            resolution.source,
            cache_state,
        )

    # The Plaky create/patch protocol is dead weight when writes are off — the agent cannot
    # act on it. Team policy stays either way so it can still EXPLAIN the conventions.
    intake_extra = TEAM_TASK_POLICY + (TASK_CREATION_WORKFLOW if allow_writes else "")
    draft_md, plaky_suffix = await asyncio.gather(
        _load_draft_markdown(session, agent_session_pk),
        _plaky_system_suffix(plaky_board_id, plaky_group_id, note=placement_note),
    )
    # Creation is reported as done because it lands in seconds. If one of those writes
    # actually failed, this turn opens by correcting it instead of leaving the user
    # believing a task exists that does not.
    plaky_suffix += await recent_failed_task_writes(session, agent_session_pk=ag.id)
    # Boardman is the team's context engine; not knowing which repos exist is how it
    # told Ali that aarflingo was not a Deepiri project.
    plaky_suffix += await org_repo_roster_markdown()

    reply: str
    assistant_tool_calls_json: str | None = None
    resolved_provider, resolved_model = _resolve_llm_context(provider, model, use_tools=use_tools)
    use_lc = bool(settings.agent_langchain_tools and use_tools)
    effective_allow_writes = allow_writes
    preview_notice = ""
    if (
        use_lc
        and allow_writes
        and settings.agent_require_confirm_bulk
        and looks_like_board_organize_request(message)
        and not has_confirm_token(message)
    ):
        effective_allow_writes = False
        preview_notice = (
            "I detected a board-organization/bulk-change request. I ran in preview mode (read-only) first.\n\n"
            "You can use **plaky_review_board** in this pass to summarize duplicates, stale items, and missing acceptance criteria.\n\n"
            "Reply with **confirm**, **go ahead**, **approve**, or **yes, apply** in your next message to enable write tools for apply.\n"
        )
    if use_lc:
        try:
            logger.info(
                "Agent chat: LangChain tool path (session_id=%s, allow_writes=%s, repo=%s)",
                sid,
                allow_writes,
                active_repo or "",
            )
            lc_hist = db_messages_to_langchain(history_msgs)
            extra = (
                f"\n\n## Tool policy\nPlaky **write** tools (create/update/comment/subtask) are "
                f"**{'ENABLED' if effective_allow_writes else 'OFF'}**. "
                "If OFF, use only list/get and GitHub/repo read tools; tell the user to pass allow_writes to enable mutations.\n"
                "If ON: you **must** run **plaky_board_schema** (and **plaky_list_workspace_users** for assignees) before "
                "**plaky_create_task** / **plaky_patch_item_fields** when field keys are not already explicit in context; "
                "the API rejects invented field keys."
            )
            if active_repo:
                extra += f"\n## Repo context\n`{active_repo}` (resolved from {resolution.source})"
            extra += repo_context_prompt
            extra += plaky_suffix
            extra += draft_md + intake_extra
            _record_context_size(extra)
            async with agent_tool_context(
                session, agent_session_pk, plaky_board_id, plaky_group_id
            ):
                tool_out = await run_tool_agent(
                    message,
                    chat_history=lc_hist,
                    allow_writes=effective_allow_writes,
                    system_extra=extra,
                    return_trace=True,
                )
                if isinstance(tool_out, tuple):
                    reply, traces = tool_out
                    assistant_tool_calls_json = _serialize_tool_calls_json(traces)
                else:
                    reply = str(tool_out)
                if preview_notice:
                    reply = preview_notice + "\n" + reply
        except Exception as e:  # noqa: BLE001 — tool agent failure falls back to plain chat
            logger.warning("LangChain tool agent failed, using plain chat: %s", e, exc_info=True)
            assistant_tool_calls_json = _runtime_error_trace(e)
            reply = await _safe_plain_chat(
                tools_unavailable=True,
                message=message,
                repo=active_repo,
                history_msgs=history_msgs,
                plaky_suffix=plaky_suffix,
                provider=provider,
                model=model,
                resolved_provider=resolved_provider,
                resolved_model=resolved_model,
                extra_system_suffix=repo_context_prompt + draft_md + intake_extra,
            )
    else:
        logger.info(
            "Agent chat: plain LLM path (single completion; session_id=%s use_tools=%s)",
            sid,
            use_tools,
        )
        reply = await _safe_plain_chat(
            message=message,
            repo=active_repo,
            history_msgs=history_msgs,
            plaky_suffix=plaky_suffix,
            provider=provider,
            model=model,
            resolved_provider=resolved_provider,
            resolved_model=resolved_model,
            extra_system_suffix=repo_context_prompt + draft_md + intake_extra,
        )

    session.add(
        AgentMessage(
            session_pk=agent_session_pk,
            role="assistant",
            content=reply,
            tool_calls_json=assistant_tool_calls_json,
        )
    )
    await session.flush()

    return reply, sid


def _sse_event(obj: Any) -> bytes:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode()


async def iter_agent_chat_sse(
    session: AsyncSession,
    *,
    message: str,
    session_id: str | None,
    repo: str | None,
    provider: str | None = None,
    model: str | None = None,
    allow_writes: bool = False,
    use_tools: bool = False,
    plaky_board_id: str | None = None,
    plaky_group_id: str | None = None,
) -> AsyncIterator[bytes]:
    """
    SSE frames with JSON payloads: session → token (many) → done | error.
    Supports both plain chat and multi-step tool agent (streaming).
    """
    sid = session_id or str(uuid.uuid4())

    q = (
        select(AgentSession)
        .where(AgentSession.session_id == sid)
        .options(selectinload(AgentSession.messages))
    )
    res = await session.execute(q)
    ag: AgentSession | None = res.scalar_one_or_none()

    resolution = resolve_repo(
        explicit_repo=repo,
        session_repo=ag.repo if ag is not None else None,
        message=message,
    )
    active_repo = resolution.repo

    if ag is None:
        ag = AgentSession(
            session_id=sid,
            repo=active_repo,
            prompt_version=settings.prompt_version,
            created_at=datetime.utcnow(),
            last_active=datetime.utcnow(),
        )
        session.add(ag)
        await session.flush()
        history_msgs: list[AgentMessage] = []
    else:
        ag.last_active = datetime.utcnow()
        if active_repo and active_repo != ag.repo:
            ag.repo = active_repo
        history_msgs = sorted(ag.messages, key=lambda m: m.id)[-settings.agent_max_history :]

    plaky_board_id, plaky_group_id, placement_note = _resolve_placement(
        plaky_board_id,
        plaky_group_id,
        active_repo,
        repo_named_but_unresolved=resolution.source == "unknown-mentioned",
    )

    # Latency plan step 1: request id + per-stage wall clocks, logged as one line.
    _req_id = uuid.uuid4().hex[:8]
    _t0 = time.monotonic()

    # The user's message goes into history BEFORE the LLM phase — a provider failure
    # (the TPM 429 in the live transcript) must not erase what the user said.
    session.add(AgentMessage(session_pk=ag.id, role="user", content=message))

    # Release the SQLite write lock NOW: the session-row insert/update above would otherwise
    # hold it through the whole (possibly minutes-long) LLM/tool phase, and every concurrent
    # agent turn would die with "database is locked" after the busy timeout.
    await session.commit()
    agent_session_pk = ag.id

    yield _sse_event({"type": "session", "session_id": sid})

    # Everything already known about this repo, assembled without a single network
    # call. Built BEFORE the router so the router can answer from it.
    project_state = await get_project_state(
        session,
        active_repo or "",
        board_id=plaky_board_id or "",
        group_id=plaky_group_id or "",
    )

    # The fast path reads Plaky live, and its client re-raises transport errors. Raised
    # from here -- outside the block that turns a failure into an SSE `error` frame -- an
    # outage truncated the stream with no error event at all, and 500'd the non-streaming
    # path. A fast path that cannot answer is not an error; it just is not a fast path.
    try:
        fast = await maybe_fast_path(
            message,
            repo=active_repo,
            board_id=plaky_board_id,
            group_id=plaky_group_id,
            state=project_state,
        )
    except Exception as exc:  # noqa: BLE001 - fall through to the full agent instead
        log_degraded(logger, "agent: fast path", exc)
        fast = None
    if fast is not None:
        session.add(AgentMessage(session_pk=ag.id, role="assistant", content=fast.reply))
        await session.flush()
        yield _sse_event({"type": "token", "text": fast.reply})
        yield _sse_event({"type": "done"})
        logger.info(
            "agent stream fast path intent=%s repo=%s source=%s",
            fast.intent,
            active_repo or "",
            resolution.source,
        )
        return

    # The model gets the answer instead of instructions for finding it.
    repo_context_prompt = render_project_state(project_state)
    from boardman.cognition.rendering import render_cognition_block

    cognition_data = (
        project_state.briefing.payload.get("cognition") if project_state.briefing.present else None
    )
    repo_context_prompt += render_cognition_block(cognition_data)
    # Serve what we have, then repair it behind the reply. Not awaited: this turn is
    # already answered, and queuing the refresh is a DB write the asker must not wait on.
    schedule_revalidation(project_state)
    cache_state = project_state.briefing.state

    # The Plaky create/patch protocol is dead weight when writes are off — the agent cannot
    # act on it. Team policy stays either way so it can still EXPLAIN the conventions.
    intake_extra = TEAM_TASK_POLICY + (TASK_CREATION_WORKFLOW if allow_writes else "")
    if active_repo:
        logger.info(
            "agent stream repo context=%s source=%s cache=%s",
            active_repo,
            resolution.source,
            cache_state,
        )
    _t_hist = time.monotonic()
    draft_md, plaky_suffix = await asyncio.gather(
        _load_draft_markdown(session, agent_session_pk),
        _plaky_system_suffix(plaky_board_id, plaky_group_id, note=placement_note),
    )
    # Creation is reported as done because it lands in seconds. If one of those writes
    # actually failed, this turn opens by correcting it instead of leaving the user
    # believing a task exists that does not.
    plaky_suffix += await recent_failed_task_writes(session, agent_session_pk=ag.id)
    # Boardman is the team's context engine; not knowing which repos exist is how it
    # told Ali that aarflingo was not a Deepiri project.
    plaky_suffix += await org_repo_roster_markdown()
    _t_ctx = time.monotonic()

    parts: list[str] = []
    resolved_provider, resolved_model = _resolve_llm_context(provider, model, use_tools=use_tools)
    use_lc = bool(settings.agent_langchain_tools and use_tools)
    effective_allow_writes = allow_writes
    preview_notice = ""
    if (
        use_lc
        and allow_writes
        and settings.agent_require_confirm_bulk
        and looks_like_board_organize_request(message)
        and not has_confirm_token(message)
    ):
        effective_allow_writes = False
        preview_notice = (
            "I detected a board-organization/bulk-change request. I ran in preview mode (read-only) first.\n\n"
            "You can use **plaky_review_board** in this pass to summarize duplicates, stale items, and missing acceptance criteria.\n\n"
            "Reply with **confirm**, **go ahead**, **approve**, or **yes, apply** in your next message to enable write tools for apply.\n"
        )

    trace_buf: list[dict[str, Any]] = []
    try:
        if use_lc:
            logger.info("Agent chat stream: LangChain tool path (session_id=%s)", sid)
            lc_hist = db_messages_to_langchain(history_msgs)
            extra = (
                f"\n\n## Tool policy\nPlaky **write** tools (create/update/comment/subtask) are "
                f"**{'ENABLED' if effective_allow_writes else 'OFF'}**. "
                "If OFF, use only list/get and GitHub/repo read tools; tell the user to pass allow_writes to enable mutations.\n"
                "If ON: you **must** run **plaky_board_schema** (and **plaky_list_workspace_users** for assignees) before "
                "**plaky_create_task** / **plaky_patch_item_fields** when field keys are not already explicit in context; "
                "the API rejects invented field keys."
            )
            if active_repo:
                extra += f"\n## Repo context\n`{active_repo}` (resolved from {resolution.source})"
            extra += repo_context_prompt
            extra += plaky_suffix
            extra += draft_md + intake_extra
            _record_context_size(extra)
            async with agent_tool_context(
                session, agent_session_pk, plaky_board_id, plaky_group_id
            ):
                async for chunk in iter_tool_agent(
                    message,
                    chat_history=lc_hist,
                    allow_writes=effective_allow_writes,
                    system_extra=extra,
                    trace_out=trace_buf,
                ):
                    if not chunk:
                        continue
                    parts.append(chunk)
                    yield _sse_event({"type": "token", "text": chunk})
        else:
            logger.info("Agent chat stream: plain LLM path (session_id=%s)", sid)
            llm_messages = _build_plain_llm_messages(
                message,
                active_repo,
                history_msgs,
                plaky_suffix,
                extra_system_suffix=repo_context_prompt + draft_md + intake_extra,
            )
            async for chunk in chat_complete_stream(llm_messages, provider=provider, model=model):
                if not chunk:
                    continue
                parts.append(chunk)
                yield _sse_event({"type": "token", "text": chunk})

        reply = "".join(parts)
        if preview_notice:
            reply = preview_notice + "\n" + reply
        assistant_tool_calls_json: str | None = None
        if use_lc and trace_buf:
            assistant_tool_calls_json = _serialize_tool_calls_json(trace_buf)
        session.add(
            AgentMessage(
                session_pk=agent_session_pk,
                role="assistant",
                content=reply,
                tool_calls_json=assistant_tool_calls_json,
            )
        )
        await session.flush()
        from boardman.agent.tool_timing import latency_percentiles, record_turn_latency

        _total = time.monotonic() - _t0
        record_turn_latency(_total)
        pct = latency_percentiles()
        logger.info(
            "agent turn %s: db+persist=%.2fs context=%.2fs llm+tools=%.2fs total=%.2fs "
            "(rolling n=%s p50=%ss p95=%ss)",
            _req_id,
            _t_hist - _t0,
            _t_ctx - _t_hist,
            _total - (_t_ctx - _t0),
            _total,
            pct["count"],
            pct["p50"],
            pct["p95"],
        )
        yield _sse_event({"type": "done"})

    except Exception as e:  # noqa: BLE001 — the SSE stream must close cleanly on any error
        logger.exception("Agent chat stream failed")
        err = _format_llm_failure(e, provider=resolved_provider, model=resolved_model)
        try:
            session.add(
                AgentMessage(
                    session_pk=ag.id,
                    role="assistant",
                    content=(
                        "(this turn failed with a provider error and produced no answer; "
                        "the user's previous message is still unhandled) " + err[:400]
                    ),
                )
            )
            await session.commit()
        except (
            Exception
        ):  # noqa: BLE001 — failure marker is best-effort; losing it means the next turn corrects
            logger.exception("could not persist failure marker")
        yield _sse_event({"type": "error", "message": err})


def _build_plain_llm_messages(
    message: str,
    repo: str | None,
    history_msgs: list[AgentMessage],
    plaky_suffix: str,
    extra_system_suffix: str = "",
) -> list[dict[str, str]]:
    llm_messages: list[dict[str, str]] = [{"role": "system", "content": BOARD_MANAGER_SYSTEM}]
    if repo:
        llm_messages[0][
            "content"
        ] += f"\n\n## Current repo context\nThe user is working with: `{repo}`."
    llm_messages[0]["content"] += plaky_suffix
    llm_messages[0]["content"] += extra_system_suffix
    for m in history_msgs:
        llm_messages.append({"role": m.role, "content": m.content})
    llm_messages.append({"role": "user", "content": message})
    return llm_messages


async def get_session_history(session: AsyncSession, session_id: str) -> list[dict[str, Any]]:
    q = (
        select(AgentSession)
        .where(AgentSession.session_id == session_id)
        .options(selectinload(AgentSession.messages))
    )
    res = await session.execute(q)
    ag = res.scalar_one_or_none()
    if not ag:
        return []
    out: list[dict[str, Any]] = []
    for m in sorted(ag.messages, key=lambda x: x.id):
        out.append(
            {
                "role": m.role,
                "content": m.content,
                "created_at": m.created_at.isoformat() if m.created_at else None,
                "content_format": "markdown" if m.role == "assistant" else "plain",
            }
        )
    return out


async def delete_agent_session(session: AsyncSession, session_id: str) -> bool:
    q = select(AgentSession).where(AgentSession.session_id == session_id)
    res = await session.execute(q)
    ag = res.scalar_one_or_none()
    if not ag:
        return False
    await session.delete(ag)
    return True
