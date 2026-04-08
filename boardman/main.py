from contextlib import asynccontextmanager

from fastapi import FastAPI

from boardman.database.session import init_db
from boardman.routes import health, github_events, tasks


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="deepiri-boardman",
        description="GitHub ↔ Plaky sync automation service",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.include_router(health.router, prefix="/api/v1")
    app.include_router(github_events.router, prefix="/api/v1")
    app.include_router(tasks.router, prefix="/api/v1")

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn
    from boardman.settings import settings

    uvicorn.run(app, host=settings.service_host, port=settings.service_port)