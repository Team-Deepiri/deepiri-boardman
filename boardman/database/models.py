from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


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


class SyncLog(Base):
    __tablename__ = "sync_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    github_repo: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    github_ref: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    plaky_task_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)