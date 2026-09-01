"""Live Postgres migration check — opt-in, needs a real Postgres reachable via
TEST_POSTGRES_URL (e.g. postgresql+asyncpg://user:pass@localhost:5432/boardman_test).

Pins two real bugs found by hand while migrating off SQLite:
  1. Alembic's version_num column is a hardcoded VARCHAR(32); several of this project's
     revision ids (the migration filenames) are longer than that. SQLite never enforces
     VARCHAR length so it was invisible there — Postgres raises
     StringDataRightTruncationError stamping the first over-length revision.
  2. `async_engine_from_config(...).connect()` does not commit DDL through to asyncpg on
     its own; alembic's own transaction wrapping did not surface the missing commit
     either (`command.upgrade` returned clean with zero tables created against Postgres).

Not run in the normal test suite (no Postgres in CI here) — this is a manual/CI-optional
guard against re-breaking the fix in alembic/env.py.
"""

from __future__ import annotations

import os

import pytest

TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL", "").strip()

pytestmark = pytest.mark.skipif(
    not TEST_POSTGRES_URL,
    reason="Set TEST_POSTGRES_URL to a real Postgres DSN to run this",
)


def test_alembic_upgrade_head_creates_all_tables_on_postgres(monkeypatch) -> None:
    # NOT async: alembic/env.py's run_migrations_online() calls asyncio.run() itself,
    # which cannot nest inside pytest-asyncio's already-running loop.
    import asyncio

    from alembic import command
    from alembic.config import Config

    # boardman.settings.settings is a singleton read once at import time — setenv alone
    # would not reach alembic/env.py's `settings.database_url` lookup.
    monkeypatch.setattr("boardman.settings.settings.database_url", TEST_POSTGRES_URL)

    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")  # leave the test DB clean; also proves downgrade path works
    command.upgrade(cfg, "head")

    import asyncpg

    dsn = TEST_POSTGRES_URL.replace("postgresql+asyncpg://", "postgresql://")

    async def _check() -> set[str]:
        conn = await asyncpg.connect(dsn)
        try:
            rows = await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            return {r["tablename"] for r in rows}
        finally:
            await conn.close()

    names = asyncio.run(_check())

    for expected in (
        "agent_sessions",
        "agent_messages",
        "sync_log",
        "issue_task_map",
        "pr_task_links",
        "scan_runs",
        "project_contexts",
        "github_webhook_deliveries",
        "alembic_version",
    ):
        assert expected in names, f"{expected} missing after alembic upgrade head on postgres"


@pytest.mark.asyncio
async def test_concurrent_writers_do_not_serialize_on_postgres(monkeypatch) -> None:
    """The whole point of the migration: N writers committing at once must not block
    each other the way SQLite's single-writer lock does."""
    import asyncio
    import time

    monkeypatch.setattr("boardman.settings.settings.database_url", TEST_POSTGRES_URL)
    import importlib

    from boardman.database import session as session_mod

    importlib.reload(session_mod)
    from boardman.database.models import SyncLog

    async def writer(n: int) -> None:
        async with session_mod.async_session() as s:
            for i in range(20):
                s.add(SyncLog(action=f"concurrent_{n}", github_repo="x/y", github_ref=str(i)))
            await s.commit()

    t0 = time.monotonic()
    await asyncio.gather(*[writer(n) for n in range(10)])
    elapsed = time.monotonic() - t0
    assert elapsed < 5.0, f"200 rows across 10 concurrent writers took {elapsed}s — investigate"
