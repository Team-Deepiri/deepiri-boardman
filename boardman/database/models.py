from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class IssueTaskMap(Base):
    __tablename__ = "issue_task_map"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    github_repo: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    github_issue_number: Mapped[int] = mapped_column(Integer, nullable=False)
    plaky_task_id: Mapped[str] = mapped_column(String(255), nullable=False)
    plaky_task_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PullRequestTaskLink(Base):
    """
    Tracks which GitHub PRs belong to which Plaky task (PR group).
    `github_issue_number=0` for fuzzy-linked PRs without an issue key.
    """

    __tablename__ = "pr_task_links"
    __table_args__ = (
        UniqueConstraint(
            "github_repo",
            "github_pr_number",
            "github_issue_number",
            name="uq_pr_task_links_repo_pr_issue",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    github_repo: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    github_pr_number: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    github_issue_number: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    plaky_task_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    link_source: Mapped[str] = mapped_column(String(32), nullable=False, default="issue_keyword")
    merged_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    withdrawn_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SyncLog(Base):
    __tablename__ = "sync_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    github_repo: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    github_ref: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    plaky_task_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class GitHubWebhookDelivery(Base):
    """Tracks processed GitHub webhook delivery IDs for replay/idempotency checks."""

    __tablename__ = "github_webhook_deliveries"

    delivery_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="processed")
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class ScanRun(Base):
    __tablename__ = "scan_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    github_repo: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    provider: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    model: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    tasks_proposed: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tasks_created: Mapped[int] = mapped_column(Integer, default=0)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AgentSession(Base):
    __tablename__ = "agent_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    repo: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    prompt_version: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    task_draft_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_active: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    messages: Mapped[list["AgentMessage"]] = relationship(
        "AgentMessage", back_populates="session", cascade="all, delete-orphan"
    )


class AgentMessage(Base):
    __tablename__ = "agent_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_pk: Mapped[int] = mapped_column(Integer, ForeignKey("agent_sessions.id"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tool_calls_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    session: Mapped["AgentSession"] = relationship("AgentSession", back_populates="messages")


class ProjectContext(Base):
    __tablename__ = "project_contexts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    repo: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    goals_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_scanned: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class OpenPRTrack(Base):
    __tablename__ = "open_pr_tracks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    repo_full_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    plaky_item_id: Mapped[str] = mapped_column(String(255), nullable=False)
    pr_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    pr_title: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class RepoTierCache(Base):
    __tablename__ = "repo_tier_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    repo_full_name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    tier: Mapped[int] = mapped_column(Integer, nullable=False)
    classified_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class BackgroundJob(Base):
    """SQLite-backed async job queue (replaces arq/Redis)."""

    __tablename__ = "background_jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", index=True
    )  # pending, running, complete, incomplete
    result_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    success: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class AgentRateLimitBucket(Base):
    """Shared leaky-bucket state (multi-process) using the same SQLite DB."""

    __tablename__ = "agent_rate_limit_buckets"

    bucket_key: Mapped[str] = mapped_column(String(768), primary_key=True)
    water: Mapped[float] = mapped_column(Float, nullable=False)
    ts: Mapped[float] = mapped_column(Float, nullable=False)  # unix seconds (wall clock)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
