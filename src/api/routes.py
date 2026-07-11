"""FastAPI REST API — stateless, DB-backed, Celery-driven.

All run state is read from WorkflowRun / WorkflowEvent in the database.
No in-memory dictionaries. Safe for horizontal scaling.

Approval flow:
  POST /pipeline/run  → creates WorkflowRun, enqueues run_pipeline_task
  GET  /pipeline/{id} → reads status from DB
  POST /pipeline/{id}/approve → writes approval to DB, enqueues resume_pipeline_task
  POST /pipeline/{id}/reject  → writes rejection to DB
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import structlog
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Security, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.database.db import get_db, init_db
from src.models.schemas import Change, Issue, Page, Site
from src.models.workflow import ApprovalRequest, NodeMetric, WorkflowEvent, WorkflowRun
from src.observability.metrics import get_node_stats
from src.services.workflow_service import WorkflowService

settings = get_settings()
logger = structlog.get_logger(__name__)

# ── Security ──────────────────────────────────────────────────────────────────

_security = HTTPBearer(auto_error=False)


def _check_api_secret(
    credentials: HTTPAuthorizationCredentials | None = Security(_security),
) -> bool:
    secret = settings.api_secret
    if not secret:
        return True
    if not credentials or credentials.credentials != secret:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing API secret")
    return True


# ── Request / Response schemas ────────────────────────────────────────────────

class RunPipelineRequest(BaseModel):
    target_url: str
    site_id: int = 1
    mode: str = "dry_run"
    max_pages: int | None = None  # None=settings default, 0=full site, N=limit to N pages
    auto_approve: bool = False


class ApproveRequest(BaseModel):
    reviewer: str = "api_user"
    notes: str = ""


class RejectRequest(BaseModel):
    reviewer: str = "api_user"
    notes: str = ""


class SiteCreateRequest(BaseModel):
    name: str
    base_url: str
    wp_username: str
    wp_app_password: str


async def _run_pipeline_fallback(run_id: str) -> None:
    """Run pipeline inside the API process when Celery is unavailable."""
    from src.worker.tasks import _mark_run_failed, _run_pipeline_async

    try:
        logger.warning("pipeline.fallback_started", run_id=run_id)
        await _run_pipeline_async(run_id)
        logger.info("pipeline.fallback_completed", run_id=run_id)
    except Exception as exc:
        logger.error("pipeline.fallback_failed", run_id=run_id, error=str(exc))
        await _mark_run_failed(run_id, str(exc))


async def _resume_pipeline_fallback(run_id: str) -> None:
    """Resume pipeline inside API process when Celery is unavailable."""
    from src.worker.tasks import _mark_run_failed, _resume_pipeline_async

    try:
        logger.warning("pipeline.resume_fallback_started", run_id=run_id)
        await _resume_pipeline_async(run_id)
        logger.info("pipeline.resume_fallback_completed", run_id=run_id)
    except Exception as exc:
        logger.error("pipeline.resume_fallback_failed", run_id=run_id, error=str(exc))
        await _mark_run_failed(run_id, str(exc))


# ── App factory ───────────────────────────────────────────────────────────────

def create_app() -> FastAPI:
    app = FastAPI(
        title="Automated SEO Agent API",
        description=(
            "Production-grade multi-agent WordPress SEO automation. "
            "Powered by Claude + LangGraph."
        ),
        version="2.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.on_event("startup")
    async def _startup():
        await init_db()
        logger.info("api.startup", host=settings.api_host, port=settings.api_port)

    # ── Health ────────────────────────────────────────────────────────────────

    @app.get("/health", tags=["System"])
    async def health_check():
        return {
            "status": "ok",
            "version": "2.0.0",
            "timestamp": datetime.utcnow().isoformat(),
            "mode": settings.execution_mode,
            "checkpoint_backend": "postgres" if settings.langgraph_checkpoint_url else "memory",
            "queue_backend": "redis" if settings.redis_url else "none",
        }

    # ── Sites ─────────────────────────────────────────────────────────────────

    @app.get("/api/v1/sites", tags=["Sites"])
    async def list_sites(
        db: AsyncSession = Depends(get_db),
        _: bool = Depends(_check_api_secret),
    ) -> list[dict]:
        result = await db.execute(select(Site).where(Site.is_active == True))
        return [
            {"id": s.id, "name": s.name, "base_url": s.base_url, "created_at": s.created_at.isoformat()}
            for s in result.scalars().all()
        ]

    @app.post("/api/v1/sites", tags=["Sites"], status_code=201)
    async def create_site(
        req: SiteCreateRequest,
        db: AsyncSession = Depends(get_db),
        _: bool = Depends(_check_api_secret),
    ) -> dict:
        site = Site(
            name=req.name,
            base_url=req.base_url.rstrip("/"),
            wp_username=req.wp_username,
            wp_app_password=req.wp_app_password,
        )
        db.add(site)
        await db.flush()
        return {"id": site.id, "name": site.name, "base_url": site.base_url}

    # ── Pipeline ──────────────────────────────────────────────────────────────

    @app.post("/api/v1/pipeline/run", tags=["Pipeline"], status_code=202)
    async def trigger_pipeline(
        req: RunPipelineRequest,
        background_tasks: BackgroundTasks,
        db: AsyncSession = Depends(get_db),
        _: bool = Depends(_check_api_secret),
    ) -> dict:
        """Enqueue a pipeline run. Returns run_id immediately.

        The Celery worker executes the pipeline asynchronously.
        Poll GET /api/v1/pipeline/{run_id} for status.
        """
        svc = WorkflowService(db)
        run = await svc.create_run(
            site_id=req.site_id,
            target_url=req.target_url,
            mode=req.mode,
            max_pages=req.max_pages,
            approval_status="approved" if req.auto_approve else "pending",
        )
        await db.commit()

        # Enqueue Celery task
        try:
            from src.worker.tasks import run_pipeline_task
            celery_result = run_pipeline_task.apply_async(
                args=[run.run_id],
                queue=settings.pipeline_queue,
            )
            # Store task ID so the cancel endpoint can revoke it
            async with db.begin_nested():
                await svc.set_celery_task_id(run.run_id, celery_result.id)
            queue_status = "enqueued"
        except Exception as exc:
            # Celery unavailable (common in local dev): run in API background.
            logger.warning("pipeline.celery_unavailable", error=str(exc))
            queue_status = "in_process_fallback"
            background_tasks.add_task(_run_pipeline_fallback, run.run_id)

        message = (
            "Pipeline enqueued. Poll GET /api/v1/pipeline/{run_id}"
            if queue_status == "enqueued"
            else "Celery unavailable. Running pipeline in API background process."
        )

        logger.info("pipeline.triggered", run_id=run.run_id, url=req.target_url)
        return {
            "run_id": run.run_id,
            "status": "accepted",
            "queue_status": queue_status,
            "message": message.format(run_id=run.run_id),
        }

    @app.get("/api/v1/pipeline/{run_id}", tags=["Pipeline"])
    async def get_pipeline_status(
        run_id: str,
        db: AsyncSession = Depends(get_db),
        _: bool = Depends(_check_api_secret),
    ) -> dict:
        """Read pipeline status from DB — no in-memory state."""
        svc = WorkflowService(db)
        run = await svc.get_run(run_id)
        if not run:
            raise HTTPException(404, "Run not found")

        response: dict[str, Any] = {
            "run_id": run.run_id,
            "thread_id": run.thread_id,
            "site_id": run.site_id,
            "target_url": run.target_url,
            "mode": run.mode,
            "max_pages": run.max_pages,
            "status": run.status,
            "current_node": run.current_node,
            "approval_status": run.approval_status,
            "started_at": run.started_at.isoformat(),
            "ended_at": run.ended_at.isoformat() if run.ended_at else None,
            "error": run.error,
        }

        if run.status == "completed" and run.result_summary:
            response["result"] = run.result_summary

        if run.status == "waiting_for_approval":
            approval = await svc.get_pending_approval(run_id)
            if approval:
                response["approval_request"] = {
                    "request_id": approval.request_id,
                    "tasks_count": approval.tasks_count,
                    "plan_summary": approval.plan_summary,
                    "expires_at": approval.expires_at.isoformat() if approval.expires_at else None,
                }

        return response

    @app.get("/api/v1/pipeline/{run_id}/events", tags=["Pipeline"])
    async def get_pipeline_events(
        run_id: str,
        db: AsyncSession = Depends(get_db),
        _: bool = Depends(_check_api_secret),
    ) -> list[dict]:
        """Return the full event log for a pipeline run."""
        svc = WorkflowService(db)
        run = await svc.get_run(run_id)
        if not run:
            raise HTTPException(404, "Run not found")
        events = await svc.get_events(run_id)
        return [
            {
                "node_name": e.node_name,
                "event_type": e.event_type,
                "duration_ms": e.duration_ms,
                "payload": e.payload,
                "timestamp": e.timestamp.isoformat(),
            }
            for e in events
        ]

    @app.get("/api/v1/pipeline/{run_id}/report", tags=["Pipeline"])
    async def get_pipeline_report(
        run_id: str,
        db: AsyncSession = Depends(get_db),
        _: bool = Depends(_check_api_secret),
    ) -> dict:
        svc = WorkflowService(db)
        run = await svc.get_run(run_id)
        if not run:
            raise HTTPException(404, "Run not found")
        if run.status != "completed":
            raise HTTPException(202, f"Run status: {run.status}")
        summary = run.result_summary or {}
        return {
            "run_id": run_id,
            "overall_seo_score": summary.get("overall_seo_score"),
            "report": summary.get("performance_report", {}),
        }

    # ── Approval ──────────────────────────────────────────────────────────────

    @app.post("/api/v1/pipeline/{run_id}/approve", tags=["Approval"])
    async def approve_pipeline(
        run_id: str,
        req: ApproveRequest,
        background_tasks: BackgroundTasks,
        db: AsyncSession = Depends(get_db),
        _: bool = Depends(_check_api_secret),
    ) -> dict:
        """Approve a waiting pipeline. Enqueues resume_pipeline_task.

        Approval flow:
        1. Write approval to DB (ApprovalRequest.status = approved)
        2. Enqueue resume_pipeline_task(run_id)
        3. Worker resumes LangGraph via same thread_id from PG checkpoint
        """
        svc = WorkflowService(db)
        run = await svc.get_run(run_id)
        if not run:
            raise HTTPException(404, "Run not found")
        if run.status != "waiting_for_approval":
            raise HTTPException(400, f"Run is not waiting for approval (status: {run.status})")

        approval = await svc.approve(run_id, reviewer=req.reviewer, notes=req.notes)
        if not approval:
            raise HTTPException(
                410, "Approval request not found or has expired"
            )
        await svc.emit_event(run_id, "human_review", "approved",
                             payload={"reviewer": req.reviewer})
        await db.commit()

        # Enqueue resume task
        queue_status = "enqueued"
        try:
            from src.worker.tasks import resume_pipeline_task
            resume_pipeline_task.apply_async(
                args=[run_id],
                queue=settings.pipeline_queue,
            )
        except Exception as exc:
            logger.warning("approve.celery_unavailable", error=str(exc))
            queue_status = "in_process_fallback"
            background_tasks.add_task(_resume_pipeline_fallback, run_id)

        message = (
            "Pipeline approved - queued for worker execution"
            if queue_status == "enqueued"
            else "Pipeline approved - resuming in API background process."
        )

        logger.info("pipeline.approved", run_id=run_id, reviewer=req.reviewer)
        return {
            "run_id": run_id,
            "status": "approved",
            "queue_status": queue_status,
            "message": message,
        }

    @app.post("/api/v1/pipeline/{run_id}/reject", tags=["Approval"])
    async def reject_pipeline(
        run_id: str,
        req: RejectRequest,
        db: AsyncSession = Depends(get_db),
        _: bool = Depends(_check_api_secret),
    ) -> dict:
        svc = WorkflowService(db)
        run = await svc.get_run(run_id)
        if not run:
            raise HTTPException(404, "Run not found")
        await svc.reject(run_id, reviewer=req.reviewer, notes=req.notes)
        await svc.emit_event(run_id, "human_review", "rejected",
                             payload={"reviewer": req.reviewer, "notes": req.notes})
        await db.commit()
        return {"run_id": run_id, "status": "rejected"}

    # ── Cancel ───────────────────────────────────────────────────────────────

    @app.post("/api/v1/pipeline/{run_id}/cancel", tags=["Pipeline"])
    async def cancel_pipeline(
        run_id: str,
        db: AsyncSession = Depends(get_db),
        _: bool = Depends(_check_api_secret),
    ) -> dict:
        """Cancel a pending or running pipeline run.

        Marks the run as cancelled in the DB and revokes the Celery task
        (sends SIGTERM to the worker process). The task's idempotency guard
        ensures it won't restart if Celery requeues it after revocation.
        """
        svc = WorkflowService(db)
        run = await svc.get_run(run_id)
        if not run:
            raise HTTPException(404, "Run not found")
        if run.status not in ("pending", "running"):
            raise HTTPException(400, f"Cannot cancel a run with status '{run.status}'")

        await svc.cancel_run(run_id)
        await svc.emit_event(run_id, "pipeline", "cancelled", payload={})
        await db.commit()

        # Best-effort Celery task revocation
        if run.celery_task_id:
            try:
                from src.worker.celery_app import celery_app as _celery
                _celery.control.revoke(run.celery_task_id, terminate=True, signal="SIGTERM")
                logger.info("pipeline.celery_revoked", run_id=run_id, task_id=run.celery_task_id)
            except Exception as exc:
                logger.warning("pipeline.revoke_failed", run_id=run_id, error=str(exc))

        logger.info("pipeline.cancelled", run_id=run_id)
        return {"run_id": run_id, "status": "cancelled"}

    # ── Runs list ─────────────────────────────────────────────────────────────

    @app.get("/api/v1/runs", tags=["Pipeline"])
    async def list_runs(
        site_id: int | None = None,
        status: str | None = None,
        limit: int = 20,
        db: AsyncSession = Depends(get_db),
        _: bool = Depends(_check_api_secret),
    ) -> list[dict]:
        svc = WorkflowService(db)
        runs = await svc.list_runs(site_id=site_id, status=status, limit=limit)
        return [
            {
                "run_id": r.run_id,
                "target_url": r.target_url,
                "mode": r.mode,
                "status": r.status,
                "current_node": r.current_node,
                "approval_status": r.approval_status,
                "started_at": r.started_at.isoformat(),
                "ended_at": r.ended_at.isoformat() if r.ended_at else None,
            }
            for r in runs
        ]

    # ── Issues ────────────────────────────────────────────────────────────────

    @app.get("/api/v1/issues", tags=["Issues"])
    async def list_issues(
        severity: str | None = None,
        issue_status: str | None = None,
        limit: int = 50,
        db: AsyncSession = Depends(get_db),
        _: bool = Depends(_check_api_secret),
    ) -> list[dict]:
        q = select(Issue).limit(limit)
        if severity:
            q = q.where(Issue.severity == severity)
        if issue_status:
            q = q.where(Issue.status == issue_status)
        result = await db.execute(q.order_by(Issue.impact_score.desc()))
        return [
            {
                "id": i.id, "rule_id": i.rule_id, "category": i.category,
                "severity": i.severity, "title": i.title,
                "description": i.description, "impact_score": i.impact_score,
                "status": i.status, "page_id": i.page_id,
            }
            for i in result.scalars().all()
        ]

    # ── Pages ─────────────────────────────────────────────────────────────────

    @app.get("/api/v1/pages", tags=["Pages"])
    async def list_pages(
        site_id: int | None = None,
        limit: int = 50,
        db: AsyncSession = Depends(get_db),
        _: bool = Depends(_check_api_secret),
    ) -> list[dict]:
        q = select(Page).limit(limit)
        if site_id:
            q = q.where(Page.site_id == site_id)
        result = await db.execute(q.order_by(Page.seo_score.asc()))
        return [
            {
                "id": p.id, "url": p.url, "title": p.title,
                "seo_score": p.seo_score, "word_count": p.word_count,
                "last_crawled_at": p.last_crawled_at.isoformat() if p.last_crawled_at else None,
            }
            for p in result.scalars().all()
        ]

    # ── Changes ───────────────────────────────────────────────────────────────

    @app.get("/api/v1/changes", tags=["Changes"])
    async def list_changes(
        limit: int = 50,
        db: AsyncSession = Depends(get_db),
        _: bool = Depends(_check_api_secret),
    ) -> list[dict]:
        result = await db.execute(
            select(Change).order_by(Change.created_at.desc()).limit(limit)
        )
        return [
            {
                "id": c.id, "change_type": c.change_type, "field_name": c.field_name,
                "risk_level": c.risk_level, "status": c.status, "page_id": c.page_id,
                "applied_at": c.applied_at.isoformat() if c.applied_at else None,
            }
            for c in result.scalars().all()
        ]

    # ── Observability ──────────────────────────────────────────────────────────

    @app.get("/api/v1/observability/nodes", tags=["Observability"])
    async def node_metrics(_: bool = Depends(_check_api_secret)) -> list[dict]:
        """Per-node execution stats: success rate, avg duration, failure count."""
        return await get_node_stats()

    @app.get("/api/v1/observability/approvals", tags=["Observability"])
    async def approval_stats(
        db: AsyncSession = Depends(get_db),
        _: bool = Depends(_check_api_secret),
    ) -> dict:
        """Approval funnel metrics."""
        result = await db.execute(select(ApprovalRequest))
        requests = result.scalars().all()
        total = len(requests)
        approved = sum(1 for r in requests if r.status == "approved")
        rejected = sum(1 for r in requests if r.status == "rejected")
        expired = sum(1 for r in requests if r.status == "expired")
        avg_delay_hours = 0.0
        decided = [r for r in requests if r.decided_at]
        if decided:
            delays = [
                (r.decided_at - r.requested_at).total_seconds() / 3600
                for r in decided
            ]
            avg_delay_hours = round(sum(delays) / len(delays), 1)
        return {
            "total_requests": total,
            "approved": approved,
            "rejected": rejected,
            "expired": expired,
            "pending": total - approved - rejected - expired,
            "avg_approval_delay_hours": avg_delay_hours,
        }

    # ── Dashboard ─────────────────────────────────────────────────────────────

    @app.get("/api/v1/dashboard", tags=["Dashboard"])
    async def dashboard(
        db: AsyncSession = Depends(get_db),
        _: bool = Depends(_check_api_secret),
    ) -> dict:
        pages_result = await db.execute(select(Page))
        all_pages = pages_result.scalars().all()

        issues_result = await db.execute(select(Issue).where(Issue.status == "open"))
        open_issues = issues_result.scalars().all()

        runs_result = await db.execute(
            select(WorkflowRun).order_by(WorkflowRun.started_at.desc()).limit(5)
        )
        recent_runs = runs_result.scalars().all()

        avg_score = (
            sum(p.seo_score for p in all_pages) / len(all_pages) if all_pages else 0.0
        )
        issue_by_severity: dict[str, int] = {}
        for i in open_issues:
            issue_by_severity[i.severity] = issue_by_severity.get(i.severity, 0) + 1

        return {
            "total_pages": len(all_pages),
            "avg_seo_score": round(avg_score, 1),
            "open_issues": len(open_issues),
            "issues_by_severity": issue_by_severity,
            "recent_runs": [
                {
                    "run_id": r.run_id,
                    "status": r.status,
                    "mode": r.mode,
                    "started_at": r.started_at.isoformat(),
                }
                for r in recent_runs
            ],
            "pages_needing_attention": [
                {"url": p.url, "seo_score": p.seo_score}
                for p in sorted(all_pages, key=lambda x: x.seo_score)[:5]
            ],
        }

    # ── Approval Review UI ────────────────────────────────────────────────────

    @app.get("/review/{run_id}", tags=["Approval UI"], response_class=HTMLResponse)
    async def approval_review_ui(
        run_id: str,
        token: str = Query(default=""),
        db: AsyncSession = Depends(get_db),
    ) -> HTMLResponse:
        """Human-readable approval page served as HTML.

        The email contains a link to this page. The manager opens it in a browser,
        reviews all proposed changes, and clicks Approve or Reject.
        The page calls the JSON API endpoints via JavaScript.

        Authentication: pass ?token=<API_SECRET> in the URL (embedded in email link).
        If no API secret is configured, the page is open.
        """
        secret = settings.api_secret
        if secret and token != secret:
            return HTMLResponse(
                content=_render_error_page("Access denied. Invalid or missing token."),
                status_code=401,
            )

        svc = WorkflowService(db)
        run = await svc.get_run(run_id)
        if not run:
            return HTMLResponse(content=_render_error_page("Run not found."), status_code=404)

        approval = await svc.get_pending_approval(run_id)
        tasks: list[dict[str, Any]] = []
        expires_at = ""
        tasks_count = 0
        risk_breakdown: dict[str, int] = {}

        if approval:
            plan = approval.plan_summary or {}
            tasks = plan.get("tasks", [])
            tasks_count = plan.get("total_tasks", len(tasks))
            risk_breakdown = plan.get("risk_breakdown", {})
            expires_at = approval.expires_at.strftime("%Y-%m-%d %H:%M UTC") if approval.expires_at else ""

        return HTMLResponse(content=_render_approval_page(
            run_id=run_id,
            target_url=run.target_url,
            mode=run.mode,
            status=run.status,
            tasks=tasks,
            tasks_count=tasks_count,
            risk_breakdown=risk_breakdown,
            expires_at=expires_at,
            token=token,
        ))

    return app


# ── Approval UI HTML renderer ─────────────────────────────────────────────────

_CHANGE_TYPE_LABELS = {
    "meta_title":       "SEO Title",
    "meta_description": "Meta Description",
    "focus_keyword":    "Focus Keyword",
    "content_rewrite":  "Content Rewrite",
    "new_section":      "New HTML Section",
    "schema":           "Schema Markup",
    "canonical_url":    "Canonical URL",
    "og_title":         "OG Title",
    "og_description":   "OG Description",
    "new_page":         "New Page",
}

_RISK_COLOURS = {"low": "#27ae60", "medium": "#e67e22", "high": "#e74c3c"}


def _risk_badge(level: str) -> str:
    colour = _RISK_COLOURS.get(level, "#95a5a6")
    return (
        f'<span style="background:{colour};color:#fff;padding:2px 10px;'
        f'border-radius:12px;font-size:11px;font-weight:700;letter-spacing:.5px;">'
        f'{level.upper()}</span>'
    )


def _render_error_page(message: str) -> str:
    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>SEO Agent</title>
<style>body{{font-family:Arial,sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;background:#f4f6f8;}}
.box{{background:#fff;padding:40px;border-radius:8px;text-align:center;box-shadow:0 2px 8px rgba(0,0,0,.1);}}
h2{{color:#e74c3c;}}p{{color:#666;}}</style></head>
<body><div class="box"><h2>⚠ Error</h2><p>{message}</p></div></body></html>"""


def _render_approval_page(
    run_id: str,
    target_url: str,
    mode: str,
    status: str,
    tasks: list[dict[str, Any]],
    tasks_count: int,
    risk_breakdown: dict[str, int],
    expires_at: str,
    token: str,
) -> str:
    # Already decided
    if status not in ("waiting_for_approval", "running", "pending"):
        label = "approved ✓" if status == "completed" else status.replace("_", " ")
        colour = "#27ae60" if status == "completed" else "#e74c3c"
        return f"""<!DOCTYPE html><html><head><meta charset="UTF-8"><title>SEO Agent — Review</title>
<style>body{{font-family:Arial,sans-serif;display:flex;align-items:center;justify-content:center;
height:100vh;margin:0;background:#f4f6f8;}}
.box{{background:#fff;padding:40px;border-radius:8px;text-align:center;
box-shadow:0 2px 8px rgba(0,0,0,.1);max-width:500px;}}
h2{{color:{colour};}}</style></head>
<body><div class="box">
<h2>This run has already been {label}.</h2>
<p style="color:#666;">Run ID: <code>{run_id}</code></p>
<p style="color:#666;">Site: {target_url}</p>
</div></body></html>"""

    # Build task rows
    rows = ""
    for i, task in enumerate(tasks):
        bg = "#f9fafb" if i % 2 == 0 else "#ffffff"
        label = _CHANGE_TYPE_LABELS.get(task.get("change_type", ""), task.get("change_type", "—"))
        old_val = task.get("old_value") or ""
        new_val = task.get("new_value") or ""
        # For content tasks, show a collapsible diff instead of truncating
        is_content = task.get("change_type") in ("new_section", "content_rewrite", "schema")
        if is_content:
            old_cell = (
                f'<details><summary style="cursor:pointer;color:#999;font-size:11px;">Current (click to expand)</summary>'
                f'<pre style="white-space:pre-wrap;font-size:11px;margin:4px 0 0;color:#c0392b;max-height:150px;overflow:auto;">'
                f'{old_val[:800] or "(empty)"}</pre></details>'
            )
            new_cell = (
                f'<details open><summary style="cursor:pointer;color:#27ae60;font-size:11px;">Proposed (click to collapse)</summary>'
                f'<pre style="white-space:pre-wrap;font-size:11px;margin:4px 0 0;color:#27ae60;max-height:150px;overflow:auto;">'
                f'{new_val[:800]}</pre></details>'
            )
        else:
            
            #old_cell = f'<span style="color:#c0392b;font-size:12px;">{old_val[:150] or "<em style=\'color:#aaa\'>—</em>"}</span>'
            old_cell = '<span style="color:#c0392b;font-size:12px;">{}</span>'.format(
                old_val.replace("\n", "<br>")
            )
            new_cell = f'<span style="color:#27ae60;font-size:12px;font-weight:500;">{new_val[:150]}</span>'

        rows += f"""<tr style="background:{bg};vertical-align:top;">
            <td style="padding:10px 14px;border-bottom:1px solid #eee;font-size:12px;max-width:200px;word-break:break-all;">
                <a href="{task.get('page_url','')}" target="_blank" style="color:#3498db;">{task.get('page_url','—')}</a>
            </td>
            <td style="padding:10px 14px;border-bottom:1px solid #eee;">{label}</td>
            <td style="padding:10px 14px;border-bottom:1px solid #eee;">{old_cell}</td>
            <td style="padding:10px 14px;border-bottom:1px solid #eee;">{new_cell}</td>
            <td style="padding:10px 14px;border-bottom:1px solid #eee;text-align:center;">{_risk_badge(task.get('risk_level','low'))}</td>
        </tr>"""

    if not rows:
        rows = '<tr><td colspan="5" style="padding:20px;text-align:center;color:#aaa;">No task details available</td></tr>'

    risk_pills = " &nbsp; ".join(
        f'{_risk_badge(lvl)} <span style="font-size:12px;color:#555;">{cnt} change{"s" if cnt != 1 else ""}</span>'
        for lvl, cnt in risk_breakdown.items() if cnt > 0
    )

    token_js = f'"{token}"' if token else '""'
    # Use relative paths so the JS works on any host (local dev or production).
    # The browser already knows the origin, so no hardcoded base URL is needed.
    approve_api = f"/api/v1/pipeline/{run_id}/approve"
    reject_api = f"/api/v1/pipeline/{run_id}/reject"
    approve_success = "Approved! Changes are being applied now. You can close this tab."

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>SEO Agent — Approve Changes</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif;
            background: #f0f2f5; color: #2c3e50; min-height: 100vh; }}
    .header {{ background: #1a252f; color: #fff; padding: 20px 32px; }}
    .header h1 {{ font-size: 20px; font-weight: 600; }}
    .header p {{ font-size: 13px; color: #95a5a6; margin-top: 4px; }}
    .container {{ max-width: 1100px; margin: 24px auto; padding: 0 20px 60px; }}
    .card {{ background: #fff; border-radius: 8px; box-shadow: 0 1px 4px rgba(0,0,0,.08);
             margin-bottom: 20px; overflow: hidden; }}
    .card-header {{ padding: 16px 20px; border-bottom: 1px solid #eee; display: flex;
                    align-items: center; justify-content: space-between; }}
    .card-header h2 {{ font-size: 15px; font-weight: 600; }}
    .meta-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px,1fr));
                  gap: 16px; padding: 20px; }}
    .meta-item label {{ font-size: 11px; color: #7f8c8d; text-transform: uppercase;
                        letter-spacing: .5px; display: block; margin-bottom: 4px; }}
    .meta-item span {{ font-size: 14px; font-weight: 500; word-break: break-all; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    thead {{ background: #f8f9fa; }}
    th {{ padding: 10px 14px; text-align: left; font-size: 11px; color: #7f8c8d;
          font-weight: 600; text-transform: uppercase; letter-spacing: .5px; }}
    .action-bar {{ background: #fff; border-radius: 8px; box-shadow: 0 1px 4px rgba(0,0,0,.08);
                   padding: 24px 20px; }}
    .action-bar h2 {{ font-size: 15px; margin-bottom: 16px; }}
    .field-row {{ margin-bottom: 14px; }}
    .field-row label {{ font-size: 12px; font-weight: 600; color: #555;
                        display: block; margin-bottom: 6px; }}
    .field-row input, .field-row textarea {{
      width: 100%; border: 1px solid #ddd; border-radius: 4px;
      padding: 8px 12px; font-size: 13px; color: #2c3e50; font-family: inherit; }}
    .field-row textarea {{ resize: vertical; height: 70px; }}
    .btn-row {{ display: flex; gap: 12px; margin-top: 20px; }}
    .btn {{ padding: 12px 28px; border: none; border-radius: 5px; font-size: 14px;
            font-weight: 700; cursor: pointer; transition: opacity .15s; letter-spacing: .3px; }}
    .btn:hover {{ opacity: .88; }}
    .btn-approve {{ background: #27ae60; color: #fff; }}
    .btn-reject  {{ background: #e74c3c; color: #fff; }}
    #feedback {{ margin-top: 16px; padding: 12px 16px; border-radius: 5px;
                 font-size: 14px; display: none; }}
    .success {{ background: #d5f5e3; color: #1e8449; }}
    .error   {{ background: #fadbd8; color: #922b21; }}
    .expires {{ font-size: 12px; color: #e74c3c; font-weight: 600; }}
  </style>
</head>
<body>

<div class="header">
  <h1>SEO Agent — Approval Review</h1>
  <p>Review the proposed changes below, then approve or reject.</p>
</div>

<div class="container">

  <!-- Run summary -->
  <div class="card">
    <div class="card-header">
      <h2>Run Summary</h2>
      {"" if not expires_at else f'<span class="expires">⏱ Expires: {expires_at}</span>'}
    </div>
    <div class="meta-grid">
      <div class="meta-item"><label>Site</label><span>{target_url}</span></div>
      <div class="meta-item"><label>Run ID</label><span style="font-size:11px;color:#7f8c8d;">{run_id}</span></div>
      <div class="meta-item"><label>Mode</label><span>{mode.upper()}</span></div>
      <div class="meta-item"><label>Total Changes</label><span style="font-size:22px;font-weight:700;">{tasks_count}</span></div>
      <div class="meta-item"><label>Risk Breakdown</label><span>{risk_pills or "—"}</span></div>
    </div>
  </div>

  <!-- Proposed changes table -->
  <div class="card">
    <div class="card-header"><h2>Proposed Changes</h2></div>
    <div style="overflow-x:auto;">
      <table>
        <thead>
          <tr>
            <th>Page URL</th>
            <th>Change Type</th>
            <th>Current Value</th>
            <th>Proposed Value</th>
            <th>Risk</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
  </div>

  <!-- Approve / Reject -->
  <div class="action-bar">
    <h2>Your Decision</h2>
    <div class="field-row">
      <label>Your name / email (for audit log)</label>
      <input type="text" id="reviewer" placeholder="e.g. John Smith" />
    </div>
    <div class="field-row">
      <label>Notes (optional)</label>
      <textarea id="notes" placeholder="Any comments about this plan..."></textarea>
    </div>
    <div class="btn-row">
      <button class="btn btn-approve" onclick="decide('approve')">✓ Approve All Changes</button>
      <button class="btn btn-reject"  onclick="decide('reject')">✗ Reject</button>
    </div>
    <div id="feedback"></div>
  </div>

</div>

<script>
  const TOKEN = {token_js};
  const APPROVE_URL = "{approve_api}";
  const REJECT_URL  = "{reject_api}";
  const APPROVE_SUCCESS = "{approve_success}";

  async function decide(action) {{
    const reviewer = document.getElementById('reviewer').value.trim() || 'anonymous';
    const notes    = document.getElementById('notes').value.trim();
    const url      = action === 'approve' ? APPROVE_URL : REJECT_URL;

    const headers = {{'Content-Type': 'application/json'}};
    if (TOKEN) headers['Authorization'] = 'Bearer ' + TOKEN;

    const fb = document.getElementById('feedback');
    fb.style.display = 'none';

    try {{
      const resp = await fetch(url, {{
        method: 'POST',
        headers,
        body: JSON.stringify({{reviewer, notes}}),
      }});
      const data = await resp.json();

      if (resp.ok) {{
        fb.className = 'success';
        fb.textContent = action === 'approve'
          ? '✓ ' + APPROVE_SUCCESS
          : '✗ Rejected. No changes will be made.';
        fb.style.display = 'block';
        // Disable buttons to prevent double-click
        document.querySelectorAll('.btn').forEach(b => b.disabled = true);
      }} else {{
        fb.className = 'error';
        fb.textContent = 'Error: ' + (data.detail || JSON.stringify(data));
        fb.style.display = 'block';
      }}
    }} catch (err) {{
      fb.className = 'error';
      fb.textContent = 'Network error: ' + err.message;
      fb.style.display = 'block';
    }}
  }}
</script>

</body>
</html>"""
