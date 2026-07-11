"""Persistent workflow tracking models — WorkflowRun, WorkflowEvent, ApprovalRequest."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.schemas import Base


def _json_col(**kwargs):
    """Use JSONB on Postgres, JSON on SQLite."""
    return mapped_column(JSONB().with_variant(JSON(), "sqlite"), **kwargs)


class WorkflowRun(Base):
    """One execution of the SEO pipeline graph for a site.

    Lifecycle:
      pending → running → waiting_for_approval → running → completed
                                               ↘ rejected → cancelled
                        → failed (at any point)
    """

    __tablename__ = "workflow_runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # LangGraph thread_id — used to resume checkpointed graph
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    site_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    target_url: Mapped[str] = mapped_column(String(500), nullable=False)
    mode: Mapped[str] = mapped_column(String(20), default="dry_run")  # dry_run|preview|live
    max_pages: Mapped[int | None] = mapped_column(Integer, nullable=True)  # None=settings default, 0=full site, N=limit
    celery_task_id: Mapped[str | None] = mapped_column(String(255), nullable=True)  # for task revocation on cancel
    status: Mapped[str] = mapped_column(
        String(40), default="pending", index=True
    )  # pending|running|waiting_for_approval|completed|failed|cancelled
    current_node: Mapped[str | None] = mapped_column(String(100), nullable=True)
    approval_status: Mapped[str] = mapped_column(
        String(20), default="pending"
    )  # pending|approved|rejected
    # Summary of the final state — populated on completion
    result_summary: Mapped[dict[str, Any] | None] = _json_col(nullable=True, default=None)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    events: Mapped[list["WorkflowEvent"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    approval_requests: Mapped[list["ApprovalRequest"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class WorkflowEvent(Base):
    """Audit trail of every node transition in a workflow run.

    Used for observability: timing, success/failure rates, approval delays.
    """

    __tablename__ = "workflow_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("workflow_runs.run_id"), nullable=False, index=True)
    node_name: Mapped[str] = mapped_column(String(100), nullable=False)
    event_type: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # node_start|node_complete|node_error|approval_requested|approved|rejected|rollback
    payload: Mapped[dict[str, Any]] = _json_col(default=dict)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)

    run: Mapped["WorkflowRun"] = relationship(back_populates="events")


class ApprovalRequest(Base):
    """A pending approval for a batch of SEO changes.

    Created when the graph pauses at human_review.
    Resolved when a user approves/rejects via the API.
    """

    __tablename__ = "approval_requests"

    request_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("workflow_runs.run_id"), nullable=False, index=True)
    # Summary of tasks for the reviewer (no PII, no secrets)
    plan_summary: Mapped[dict[str, Any]] = _json_col(default=dict)
    tasks_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(
        String(20), default="pending"
    )  # pending|approved|rejected|expired
    reviewer: Mapped[str | None] = mapped_column(String(255), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    run: Mapped["WorkflowRun"] = relationship(back_populates="approval_requests")


class NodeMetric(Base):
    """Aggregated per-node performance metrics (updated after each run).

    Enables long-term observability: which nodes are slow / fail often.
    """

    __tablename__ = "node_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    node_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    total_runs: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    avg_duration_ms: Mapped[float] = mapped_column(Float, default=0.0)
    max_duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
