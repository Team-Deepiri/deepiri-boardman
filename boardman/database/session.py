"""SQLAlchemy async engine and session factory (SQLite for local/dev, Postgres for
multi-writer deployments — see database_url in boardman.settings)."""

from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from boardman.settings import settings

# Validate a pooled connection before handing it out. A cancelled request (the client
# closing an SSE stream mid-answer is the common one) can leave its aiosqlite connection
# closed but still pooled; the next checkout then died on its FIRST query with
# "no active connection", and every later chat turn hit the same poisoned connection
# until the process restarted. The check is a trivial round trip on a local file.
_engine_kwargs: dict[str, object] = {"echo": False, "pool_pre_ping": True}
if settings.database_url.startswith("sqlite"):
    # Wait longer on write contention instead of failing fast with "database is locked".
    _engine_kwargs["connect_args"] = {"timeout": 30}

engine = create_async_engine(settings.database_url, **_engine_kwargs)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


if settings.database_url.startswith("sqlite"):

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        cur = dbapi_connection.cursor()
        try:
            # WAL improves read/write concurrency for API + worker access.
            cur.execute("PRAGMA journal_mode=WAL")
            # Keep writer waiting a bit before returning "database is locked".
            cur.execute("PRAGMA busy_timeout=30000")
        finally:
            cur.close()


async def get_db() -> AsyncIterator[AsyncSession]:
    async with async_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:  # noqa: BLE001 - graceful degradation
            await session.rollback()
            raise


async def init_db() -> None:
    from sqlalchemy import text

    from boardman.database.models import Base

    async with engine.begin() as conn:
        # create_all is a fresh-install convenience for local/dev SQLite only. On
        # Postgres it must never run: every Postgres deployment is expected to run
        # `alembic upgrade head` explicitly (see docs/DEPLOYMENT.md), and if the app
        # container starts concurrently with that migration — the ordinary case in a
        # docker-compose stack where both depend_on the same postgres healthcheck —
        # create_all wins the race, creates every table itself, and alembic's own
        # 001_initial migration then fails with DuplicateTableError trying to create
        # tables that already exist. Skipping it here removes the race outright
        # rather than trying to order around it.
        if settings.database_url.startswith("sqlite"):
            await conn.run_sync(Base.metadata.create_all)

            def _add_optional_columns(sync_conn):  # rolling-deploy shim; remove once all instances run alembic upgrade head
                r = sync_conn.execute(text("PRAGMA table_info(agent_sessions)"))
                cols = [row[1] for row in r.fetchall()]
                if "task_draft_json" not in cols:
                    sync_conn.execute(
                        text("ALTER TABLE agent_sessions ADD COLUMN task_draft_json TEXT")
                    )
                if "byok_provider" not in cols:
                    sync_conn.execute(
                        text("ALTER TABLE agent_sessions ADD COLUMN byok_provider VARCHAR(32)")
                    )
                if "byok_key_encrypted" not in cols:
                    sync_conn.execute(
                        text("ALTER TABLE agent_sessions ADD COLUMN byok_key_encrypted TEXT")
                    )
                if "byok_key_expires_at" not in cols:
                    sync_conn.execute(
                        text(
                            "ALTER TABLE agent_sessions ADD COLUMN byok_key_expires_at DATETIME"
                        )
                    )

                r = sync_conn.execute(text("PRAGMA table_info(project_contexts)"))
                context_cols = [row[1] for row in r.fetchall()]
                additions = {
                    "context_json": "TEXT",
                    "context_source_revision": "VARCHAR(255)",
                    "context_fetched_at": "DATETIME",
                }
                for name, sql_type in additions.items():
                    if name not in context_cols:
                        sync_conn.execute(
                            text(f"ALTER TABLE project_contexts ADD COLUMN {name} {sql_type}")
                        )

                # `create_all` skips a table that already exists, so a column added to one
                # never appears on a database that predates it -- and every PR webhook then
                # fails with "no such column" the moment a link is read. Migration 007 adds
                # this properly; the shim is what keeps an instance running from an older
                # file working before anybody runs `alembic upgrade head`.
                #
                # TEMPORARY, by design: every ALTER here has a matching Alembic migration
                # already checked in. Safe to delete this whole function once every
                # instance this app runs on has had `alembic upgrade head` run against it
                # at least once -- until then, deleting it breaks any instance that hasn't.
                r = sync_conn.execute(text("PRAGMA table_info(pr_task_links)"))
                link_cols = [row[1] for row in r.fetchall()]
                if link_cols and "withdrawn_github_at" not in link_cols:
                    sync_conn.execute(
                        text("ALTER TABLE pr_task_links ADD COLUMN withdrawn_github_at VARCHAR(40)")
                    )
                if link_cols and "qa_plaky_id" not in link_cols:
                    sync_conn.execute(
                        text("ALTER TABLE pr_task_links ADD COLUMN qa_plaky_id VARCHAR(255)")
                    )
                if link_cols and "commits_at_last_review" not in link_cols:
                    sync_conn.execute(
                        text(
                            "ALTER TABLE pr_task_links ADD COLUMN commits_at_last_review INTEGER"
                        )
                    )

                # `create_all` skips an existing table, so the unique constraint that stops
                # two concurrent deliveries filing two cards for one issue never reached a
                # database made before it. The reservation guard in `handle_issue_opened`
                # relies on the IntegrityError this raises; without the index it raises
                # nothing and both deliveries create a card. Duplicates are cleared first,
                # newest kept, exactly as migration 006 does.
                have = sync_conn.execute(
                    text(
                        "SELECT name FROM sqlite_master WHERE type='index' "
                        "AND name='uq_issue_task_map_repo_issue'"
                    )
                ).fetchone()
                if not have:
                    sync_conn.execute(
                        text(
                            "DELETE FROM issue_task_map WHERE id NOT IN ("
                            "  SELECT MAX(id) FROM issue_task_map "
                            "  GROUP BY github_repo, github_issue_number)"
                        )
                    )
                    sync_conn.execute(
                        text(
                            "CREATE UNIQUE INDEX IF NOT EXISTS uq_issue_task_map_repo_issue "
                            "ON issue_task_map (github_repo, github_issue_number)"
                        )
                    )

            await conn.run_sync(_add_optional_columns)
