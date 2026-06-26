import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from boardman.assignment.config import sync_team_assignment_field_keys_from_board
from boardman.broker.job_queue import close_job_queue
from boardman.cache.agent_redis import aclose_agent_redis
from boardman.database.session import init_db
from boardman.logging_config import setup_logging
from boardman.llm.completion import aclose_ollama_http_client
from boardman.llm.ollama_autodetect import effective_ollama_model
from boardman.ratelimit.leaky_bucket import get_agent_leaky_limiter
from boardman.routes import agent, assignment, health, github_events, github_team, plaky, tasks, repos
from boardman.settings import settings

_log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    await init_db()
    if settings.agent_rate_limit_enabled:
        try:
            await get_agent_leaky_limiter()
        except Exception as e:
            _log.warning("Agent rate limiter init skipped: %s", e)
    pk = (settings.plaky_api_key or "").strip()
    if pk:
        _log.info("Plaky: API key present (length=%d), base=%s", len(pk), settings.plaky_api_base)
    else:
        _log.warning(
            "Plaky: PLAKY_API_KEY is empty — set it in `.env` (docker: env_file) or the environment. "
            "Boards/match and agent Plaky tools will not call the API."
        )
    if pk and settings.plaky_auto_sync_team_assignment_field_keys:
        from boardman.repos_config import team_assignment_field_sync_board_id

        bid = team_assignment_field_sync_board_id()
        if bid:
            try:
                synced = await sync_team_assignment_field_keys_from_board(bid)
                if synced.get("updated"):
                    _log.info("team_assignments: synced field keys from board %s -> %s", bid, synced.get("updated"))
                else:
                    _log.info("team_assignments: field-key sync skipped (%s)", synced.get("message", "no changes"))
            except Exception as e:
                _log.warning("team_assignments: startup field-key sync failed: %s", e)
        else:
            _log.info("team_assignments: startup field-key sync skipped (repos.yml defaults.plaky_board_id empty)")
    prov = (settings.llm_provider or "ollama").lower()
    if prov == "ollama":
        try:
            em = effective_ollama_model(None)
            src = "LLM_MODEL" if (settings.llm_model or "").strip() else "auto /api/tags"
            _log.info(
                "Agent LLM: provider=ollama model=%s (%s) ollama_base=%s",
                em,
                src,
                settings.ollama_base_url,
            )
        except Exception as e:
            _log.warning("Agent LLM: could not resolve Ollama model at startup: %s", e)
    else:
        _log.info(
            "Agent LLM: provider=%s model=%s ollama_base=%s",
            settings.llm_provider,
            (settings.llm_model or "").strip() or "(provider default)",
            settings.ollama_base_url,
        )
    if (settings.agent_redis_url or "").strip():
        _log.info("Agent Redis cache: AGENT_REDIS_URL is set (API-only; worker should leave it empty)")
    yield

    await close_job_queue()
    await aclose_agent_redis()
    await aclose_ollama_http_client()


def create_app() -> FastAPI:
    app = FastAPI(
        title="deepiri-boardman",
        description="GitHub ↔ Plaky sync automation service",
        version="0.1.0",
        lifespan=lifespan,
    )

    origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # Always-on backend worker surface: health + GitHub webhooks + QA assignment.
    app.include_router(health.router, prefix="/api/v1")
    app.include_router(github_events.router, prefix="/api/v1")
    app.include_router(assignment.router, prefix="/api/v1")

    # Conversational agent (chat/scan/routing) + UI-supporting REST. Disabled in the
    # worker-only production deployment via BOARDMAN_ENABLE_AGENT_API=false.
    if settings.boardman_enable_agent_api:
        app.include_router(github_team.router, prefix="/api/v1")
        app.include_router(tasks.router, prefix="/api/v1")
        app.include_router(plaky.router, prefix="/api/v1")
        app.include_router(agent.router, prefix="/api/v1")
        app.include_router(repos.router, prefix="/api/v1")
    else:
        _log.info("Agent/UI API routes disabled (BOARDMAN_ENABLE_AGENT_API=false): worker-only mode")

    return app


app = create_app()


if __name__ == "__main__":
    import os

    import uvicorn

    reload = os.environ.get("UVICORN_RELOAD", "").strip().lower() in ("1", "true", "yes")
    if reload:
        boardman_pkg = os.path.dirname(os.path.abspath(__file__))
        uvicorn.run(
            "boardman.main:app",
            host=settings.service_host,
            port=settings.service_port,
            reload=True,
            reload_dirs=[boardman_pkg],
            log_level=settings.log_level.lower(),
        )
    else:
        uvicorn.run(
            app,
            host=settings.service_host,
            port=settings.service_port,
            log_level=settings.log_level.lower(),
        )
