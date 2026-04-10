from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from boardman.database.session import init_db
from boardman.logging_config import setup_logging
from boardman.routes import agent, health, github_events, plaky, tasks
from boardman.settings import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    await init_db()
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
    app.include_router(tasks.router, prefix="/api/v1")
    app.include_router(plaky.router, prefix="/api/v1")
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