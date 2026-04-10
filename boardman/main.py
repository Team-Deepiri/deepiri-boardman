import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from boardman.database.session import init_db
from boardman.logging_config import setup_logging
from boardman.routes import agent, assignment, health, github_events, github_team, plaky, tasks
from boardman.llm.ollama_autodetect import effective_ollama_model
from boardman.settings import settings

_log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    await init_db()
    pk = (settings.plaky_api_key or "").strip()
    if pk:
        _log.info("Plaky: API key present (length=%d), base=%s", len(pk), settings.plaky_api_base)
    else:
        _log.warning(
            "Plaky: PLAKY_API_KEY is empty — set it in `.env` (docker: env_file) or the environment. "
            "Boards/match and agent Plaky tools will not call the API."
        )
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
    yield


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

    app.include_router(health.router, prefix="/api/v1")
    app.include_router(github_events.router, prefix="/api/v1")
    app.include_router(github_team.router, prefix="/api/v1")
    app.include_router(tasks.router, prefix="/api/v1")
    app.include_router(plaky.router, prefix="/api/v1")
    app.include_router(assignment.router, prefix="/api/v1")
    app.include_router(agent.router, prefix="/api/v1")

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