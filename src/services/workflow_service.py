"""WorkflowService — single source of truth for pipeline run state.

All status reads and writes go through this service.
The API never holds run state in memory.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.models.workflow import ApprovalRequest, WorkflowEvent, WorkflowRun

settings = get_settings()
logger = structlog.get_logger(__name__)


class WorkflowService:
    """CRUD + state-machine operations for WorkflowRun records."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ── Create ────────────────────────────────────────────────────────────────

    async def create_run(
        self,
        site_id: int,
        target_url: str,
        mode: str,
        run_id: str | None = None,
        max_pages: int | None = None,
        approval_status: str = "pending",
    ) -> WorkflowRun:
        """Create a new WorkflowRun in 'pending' state.

        A unique thread_id is generated alongside the run_id.
        The thread_id is what LangGraph uses for checkpoint resumption.
        max_pages: None=use settings default, 0=full site, N=limit to N pages.
        """
        _run_id = run_id or str(uuid.uuid4())
        run = WorkflowRun(
            run_id=_run_id,
            thread_id=_run_id,   # 1:1 mapping; thread_id == run_id for simplicity
            site_id=site_id,
            target_url=target_url,
            mode=mode,
            max_pages=max_pages,
            status="pending",
            approval_status=approval_status,
        )
        self.db.add(run)
        await self.db.flush()
        logger.info("workflow_service.create_run", run_id=_run_id, site_id=site_id)
        return run

    # ── Read ──────────────────────────────────────────────────────────────────

    async def get_run(self, run_id: str) -> WorkflowRun | None:
        return await self.db.get(WorkflowRun, run_id)

    async def list_runs(
        self,
        site_id: int | None = None,
        status: str | None = None,
        limit: int = 20,
    ) -> list[WorkflowRun]:
        q = select(WorkflowRun).order_by(WorkflowRun.started_at.desc()).limit(limit)
        if site_id:
            q = q.where(WorkflowRun.site_id == site_id)
        if status:
            q = q.where(WorkflowRun.status == status)
        result = await self.db.execute(q)
        return list(result.scalars().all())

    async def get_last_completed_run(self, site_id: int, exclude_run_id: str | None = None) -> WorkflowRun | None:
        """Return the most recent completed run for a site (excluding the current one)."""
        q = (
            select(WorkflowRun)
            .where(WorkflowRun.site_id == site_id)
            .where(WorkflowRun.status == "completed")
        )
        if exclude_run_id:
            q = q.where(WorkflowRun.run_id != exclude_run_id)
        q = q.order_by(WorkflowRun.ended_at.desc()).limit(1)
        result = await self.db.execute(q)
        return result.scalar_one_or_none()

    async def get_pending_approval(self, run_id: str) -> ApprovalRequest | None:
        result = await self.db.execute(
            select(ApprovalRequest)
            .where(ApprovalRequest.run_id == run_id)
            .where(ApprovalRequest.status == "pending")
            .order_by(ApprovalRequest.requested_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_latest_approval(
        self, run_id: str, status: str | None = None
    ) -> ApprovalRequest | None:
        q = (
            select(ApprovalRequest)
            .where(ApprovalRequest.run_id == run_id)
            .order_by(ApprovalRequest.requested_at.desc())
            .limit(1)
        )
        if status:
            q = q.where(ApprovalRequest.status == status)
        result = await self.db.execute(q)
        return result.scalar_one_or_none()

    # ── Transitions ───────────────────────────────────────────────────────────

    async def mark_running(self, run_id: str, node: str | None = None) -> None:
        await self.db.execute(
            update(WorkflowRun)
            .where(WorkflowRun.run_id == run_id)
            .values(status="running", current_node=node)
        )

    async def set_celery_task_id(self, run_id: str, task_id: str) -> None:
        await self.db.execute(
            update(WorkflowRun)
            .where(WorkflowRun.run_id == run_id)
            .values(celery_task_id=task_id)
        )

    async def cancel_run(self, run_id: str) -> None:
        await self.db.execute(
            update(WorkflowRun)
            .where(WorkflowRun.run_id == run_id)
            .values(status="cancelled", ended_at=datetime.utcnow())
        )
        logger.info("workflow_service.cancel_run", run_id=run_id)

    async def mark_waiting_approval(
        self, run_id: str, tasks: list[dict[str, Any]]
    ) -> ApprovalRequest:
        """Pause a workflow at human_review and create an ApprovalRequest."""
        await self.db.execute(
            update(WorkflowRun)
            .where(WorkflowRun.run_id == run_id)
            .values(status="waiting_for_approval", current_node="human_review")
        )

        expiry = datetime.utcnow() + timedelta(hours=settings.policy_approval_expiry_hours)
        plan_summary = {
            "tasks": [
                {
                    "task_id":     t.get("task_id"),
                    "change_type": t.get("change_type"),
                    "page_url":    t.get("page_url"),
                    "risk_level":  t.get("risk_level"),
                    "field_name":  t.get("field_name"),
                    "old_value":   t.get("old_value"),          # current content on site
                    "new_value":   t.get("new_value", "")[:500],  # AI-generated content (capped at 500 chars)
                    "wp_meta_key": t.get("wp_meta_key"),        # e.g. _yoast_wpseo_title
                    "wp_post_id":  t.get("wp_post_id"),
                    "post_type":   t.get("post_type"),
                }
                for t in tasks[:50]
            ],
            # Full payload used as a checkpoint fallback for preview approvals.
            # The visible review table above stays capped, but execution must
            # retain complete HTML and WordPress IDs.
            "execution_tasks": tasks,
            "total_tasks": len(tasks),
            "risk_breakdown": _count_risks(tasks),
        }
        request = ApprovalRequest(
            request_id=str(uuid.uuid4()),
            run_id=run_id,
            plan_summary=plan_summary,
            tasks_count=len(tasks),
            status="pending",
            expires_at=expiry,
        )
        self.db.add(request)
        await self.db.flush()
        logger.info(
            "workflow_service.waiting_approval",
            run_id=run_id,
            tasks=len(tasks),
            expires_at=expiry.isoformat(),
        )
        return request

    async def approve(
        self, run_id: str, reviewer: str, notes: str = ""
    ) -> ApprovalRequest | None:
        request = await self.get_pending_approval(run_id)
        if not request:
            return None
        if request.expires_at and datetime.utcnow() > request.expires_at:
            await self._expire_request(request)
            return None

        request.status = "approved"
        request.reviewer = reviewer
        request.notes = notes
        request.decided_at = datetime.utcnow()

        await self.db.execute(
            update(WorkflowRun)
            .where(WorkflowRun.run_id == run_id)
            .values(approval_status="approved")
        )
        logger.info("workflow_service.approved", run_id=run_id, reviewer=reviewer)
        return request

    async def reject(
        self, run_id: str, reviewer: str, notes: str = ""
    ) -> ApprovalRequest | None:
        request = await self.get_pending_approval(run_id)
        if request:
            request.status = "rejected"
            request.reviewer = reviewer
            request.notes = notes
            request.decided_at = datetime.utcnow()

        await self.db.execute(
            update(WorkflowRun)
            .where(WorkflowRun.run_id == run_id)
            .values(status="cancelled", approval_status="rejected")
        )
        logger.info("workflow_service.rejected", run_id=run_id, reviewer=reviewer)
        return request

    async def mark_completed(
        self, run_id: str, result_summary: dict[str, Any]
    ) -> None:
        await self.db.execute(
            update(WorkflowRun)
            .where(WorkflowRun.run_id == run_id)
            .values(
                status="completed",
                ended_at=datetime.utcnow(),
                result_summary=result_summary,
                current_node="END",
            )
        )

    async def mark_failed(self, run_id: str, error: str) -> None:
        await self.db.execute(
            update(WorkflowRun)
            .where(WorkflowRun.run_id == run_id)
            .values(status="failed", ended_at=datetime.utcnow(), error=error[:2000])
        )

    async def update_node(self, run_id: str, node_name: str) -> None:
        await self.db.execute(
            update(WorkflowRun)
            .where(WorkflowRun.run_id == run_id)
            .values(current_node=node_name)
        )

    # ── Events ────────────────────────────────────────────────────────────────

    async def emit_event(
        self,
        run_id: str,
        node_name: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        duration_ms: int | None = None,
    ) -> None:
        event = WorkflowEvent(
            run_id=run_id,
            node_name=node_name,
            event_type=event_type,
            payload=payload or {},
            duration_ms=duration_ms,
        )
        self.db.add(event)
        # Fire and forget — do not block the pipeline on this write

    async def get_events(self, run_id: str) -> list[WorkflowEvent]:
        result = await self.db.execute(
            select(WorkflowEvent)
            .where(WorkflowEvent.run_id == run_id)
            .order_by(WorkflowEvent.timestamp.asc())
        )
        return list(result.scalars().all())

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _expire_request(self, request: ApprovalRequest) -> None:
        request.status = "expired"
        request.decided_at = datetime.utcnow()
        await self.db.execute(
            update(WorkflowRun)
            .where(WorkflowRun.run_id == request.run_id)
            .values(status="cancelled", approval_status="rejected")
        )


def _count_risks(tasks: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {"low": 0, "medium": 0, "high": 0}
    for t in tasks:
        level = t.get("risk_level", "low")
        counts[level] = counts.get(level, 0) + 1
    return counts
