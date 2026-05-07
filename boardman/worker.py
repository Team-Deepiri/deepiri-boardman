"""Deprecated: arq/Redis worker removed. Use ``python -m boardman.sqlite_worker``."""

from __future__ import annotations

# Re-export job handlers for tests or tooling that imported from worker.
from boardman.jobs.handlers import JOB_HANDLERS, boardman_agent_chat_job, plaky_reorder_group_job

__all__ = [
    "JOB_HANDLERS",
    "boardman_agent_chat_job",
    "plaky_reorder_group_job",
]
