"""SQLAlchemy ORM models for the SEO agent database."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ── Site ─────────────────────────────────────────────────────────────────────

class Site(Base):
    """Represents a WordPress site being managed."""

    __tablename__ = "sites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    base_url: Mapped[str] = mapped_column(String(500), nullable=False, unique=True)
    wp_username: Mapped[str] = mapped_column(String(255), nullable=False)
    # Encrypted in production — stored as plain for dev simplicity
    wp_app_password: Mapped[str] = mapped_column(String(500), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    crawl_sessions: Mapped[list["CrawlSession"]] = relationship(
        back_populates="site", cascade="all, delete-orphan"
    )
    pages: Mapped[list["Page"]] = relationship(
        back_populates="site", cascade="all, delete-orphan"
    )


# ── Crawl Session ────────────────────────────────────────────────────────────

class CrawlSession(Base):
    """One crawl run for a site."""

    __tablename__ = "crawl_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(
        String(50), default="running"
    )  # running | completed | failed
    pages_crawled: Mapped[int] = mapped_column(Integer, default=0)
    pages_failed: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True)

    site: Mapped["Site"] = relationship(back_populates="crawl_sessions")
    issues: Mapped[list["Issue"]] = relationship(
        back_populates="crawl_session", cascade="all, delete-orphan"
    )


# ── Page ─────────────────────────────────────────────────────────────────────

class Page(Base):
    """A crawled / known page on the site."""

    __tablename__ = "pages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), nullable=False)
    wp_post_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    url: Mapped[str] = mapped_column(String(2000), nullable=False)
    post_type: Mapped[str] = mapped_column(String(50), default="page")  # page | post
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    meta_title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    meta_description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    focus_keyword: Mapped[str | None] = mapped_column(String(255), nullable=True)
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    seo_score: Mapped[float] = mapped_column(Float, default=0.0)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    load_time_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_crawled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_changed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    site: Mapped["Site"] = relationship(back_populates="pages")
    issues: Mapped[list["Issue"]] = relationship(
        back_populates="page", cascade="all, delete-orphan"
    )
    snapshots: Mapped[list["PageSnapshot"]] = relationship(
        back_populates="page", cascade="all, delete-orphan"
    )
    changes: Mapped[list["Change"]] = relationship(
        back_populates="page", cascade="all, delete-orphan"
    )
    ranking_records: Mapped[list["RankingRecord"]] = relationship(
        back_populates="page", cascade="all, delete-orphan"
    )


# ── Issue ────────────────────────────────────────────────────────────────────

class Issue(Base):
    """An SEO issue found on a page."""

    __tablename__ = "issues"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    crawl_session_id: Mapped[int] = mapped_column(
        ForeignKey("crawl_sessions.id"), nullable=False
    )
    page_id: Mapped[int] = mapped_column(ForeignKey("pages.id"), nullable=False)
    rule_id: Mapped[str] = mapped_column(String(100), nullable=False)
    category: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # meta | content | technical | structure | performance | schema
    severity: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # critical | high | medium | low
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    current_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    suggested_fix: Mapped[str | None] = mapped_column(Text, nullable=True)
    impact_score: Mapped[float] = mapped_column(Float, default=0.0)  # 0–100
    status: Mapped[str] = mapped_column(
        String(30), default="open"
    )  # open | in_progress | resolved | ignored
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    crawl_session: Mapped["CrawlSession"] = relationship(back_populates="issues")
    page: Mapped["Page"] = relationship(back_populates="issues")


# ── Execution Plan ────────────────────────────────────────────────────────────

class ExecutionPlan(Base):
    """A batch of changes to be executed, awaiting approval."""

    __tablename__ = "execution_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(
        String(30), default="pending_approval"
    )  # pending_approval | approved | rejected | executing | completed | failed
    total_changes: Mapped[int] = mapped_column(Integer, default=0)
    applied_changes: Mapped[int] = mapped_column(Integer, default=0)
    ai_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    executed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    changes: Mapped[list["Change"]] = relationship(
        back_populates="plan", cascade="all, delete-orphan"
    )


# ── Change ───────────────────────────────────────────────────────────────────

class Change(Base):
    """A single change to be applied or already applied to a page."""

    __tablename__ = "changes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("execution_plans.id"), nullable=False)
    page_id: Mapped[int] = mapped_column(ForeignKey("pages.id"), nullable=False)
    issue_id: Mapped[int | None] = mapped_column(ForeignKey("issues.id"), nullable=True)
    change_type: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # meta_title | meta_description | content | schema | new_page | internal_link
    risk_level: Mapped[str] = mapped_column(
        String(20), default="low"
    )  # low | medium | high
    field_name: Mapped[str] = mapped_column(String(255), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str] = mapped_column(Text, nullable=False)
    wp_meta_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(
        String(30), default="pending"
    )  # pending | applied | failed | rolled_back
    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    plan: Mapped["ExecutionPlan"] = relationship(back_populates="changes")
    page: Mapped["Page"] = relationship(back_populates="changes")
    validations: Mapped[list["ValidationResult"]] = relationship(
        back_populates="change", cascade="all, delete-orphan"
    )


# ── Validation Result ────────────────────────────────────────────────────────

class ValidationResult(Base):
    """Post-change validation outcome."""

    __tablename__ = "validation_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    change_id: Mapped[int] = mapped_column(ForeignKey("changes.id"), nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    checks: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    validated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    change: Mapped["Change"] = relationship(back_populates="validations")


# ── Page Snapshot ─────────────────────────────────────────────────────────────

class PageSnapshot(Base):
    """Snapshot of a page's SEO state at a point in time (for diffing/rollback)."""

    __tablename__ = "page_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    page_id: Mapped[int] = mapped_column(ForeignKey("pages.id"), nullable=False)
    snapshot_type: Mapped[str] = mapped_column(String(30), default="pre_change")
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    taken_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    page: Mapped["Page"] = relationship(back_populates="snapshots")


# ── Ranking Record ────────────────────────────────────────────────────────────

class RankingRecord(Base):
    """SERP ranking for a keyword on a page, recorded over time."""

    __tablename__ = "ranking_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    page_id: Mapped[int] = mapped_column(ForeignKey("pages.id"), nullable=False)
    keyword: Mapped[str] = mapped_column(String(500), nullable=False)
    position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    impressions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    clicks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ctr: Mapped[float | None] = mapped_column(Float, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    page: Mapped["Page"] = relationship(back_populates="ranking_records")
