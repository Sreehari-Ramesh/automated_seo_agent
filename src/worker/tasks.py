"""Celery tasks — async pipeline execution wrapped in sync Celery tasks.

Each task uses asyncio.run() to drive async LangGraph code inside
the sync Celery worker. This is the standard pattern for Celery + asyncio.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import structlog
from celery import shared_task

from src.config import get_settings
from src.worker.celery_app import celery_app

settings = get_settings()
logger = structlog.get_logger(__name__)

_SUMMARY_MAX_PAGES = 3
_SUMMARY_MAX_TYPES = 3
_SUMMARY_MAX_ISSUES = 5


# ─── Task 1: run_pipeline_task ────────────────────────────────────────────────

@celery_app.task(
    bind=True,
    name="src.worker.tasks.run_pipeline_task",
    max_retries=3,
    default_retry_delay=60,
    soft_time_limit=settings.worker_soft_time_limit,
    time_limit=settings.worker_hard_time_limit,
)
def run_pipeline_task(self, run_id: str) -> dict[str, Any]:
    """Execute the full SEO pipeline for a given run_id.

    Reads WorkflowRun from DB, runs the LangGraph pipeline,
    pauses at human_review (graph interrupt), updates DB status.

    Args:
        run_id: The WorkflowRun.run_id (also used as LangGraph thread_id).
    """
    try:
        return asyncio.run(_run_pipeline_async(run_id))
    except Exception as exc:
        logger.error("run_pipeline_task.failed", run_id=run_id, error=str(exc))
        asyncio.run(_mark_run_failed(run_id, str(exc)))
        raise self.retry(exc=exc)


async def _run_pipeline_async(run_id: str) -> dict[str, Any]:
    from src.database.db import AsyncSessionLocal
    from src.services.workflow_service import WorkflowService
    from src.graph.seo_graph import get_compiled_graph, build_initial_state
    from src.observability.metrics import record_node_event

    async with AsyncSessionLocal() as db:
        svc = WorkflowService(db)
        run = await svc.get_run(run_id)
        if not run:
            raise ValueError(f"WorkflowRun {run_id} not found")

        # Idempotency guard: Celery requeues tasks when the worker dies mid-run
        # (task_acks_late + task_reject_on_worker_lost). Skip if the run already
        # reached a terminal or awaiting state so we don't re-execute finished work.
        _SKIP_STATUSES = {"completed", "waiting_for_approval", "cancelled", "rejected"}
        if run.status in _SKIP_STATUSES:
            logger.info(
                "run_pipeline_async.skip_already_terminal",
                run_id=run_id,
                status=run.status,
            )
            return {"status": run.status, "skipped": True}

        await svc.mark_running(run_id, node="crawler")
        await db.commit()

    logger.info("run_pipeline_async.start", run_id=run_id, url=run.target_url)

    # Load previous run context so agents know what was done last cycle
    previous_run_context: dict[str, Any] = {}
    async with AsyncSessionLocal() as db:
        svc = WorkflowService(db)
        prev = await svc.get_last_completed_run(run.site_id, exclude_run_id=run_id)
        if prev and prev.result_summary:
            s = prev.result_summary
            previous_run_context = {
                "run_id": prev.run_id,
                "completed_at": prev.ended_at.isoformat() if prev.ended_at else "",
                "seo_score": s.get("overall_seo_score", 0),
                "issues_found": s.get("issues_found", 0),
                "changes_applied": s.get("changes_applied", 0),
                "pages_changed": s.get("pages_changed", [])[:_SUMMARY_MAX_PAGES],
                "applied_change_types": s.get("applied_change_types", [])[:_SUMMARY_MAX_TYPES],
                "top_issues": s.get("top_issues", [])[:_SUMMARY_MAX_ISSUES],
                "keywords_targeted": s.get("keywords_targeted", [])[:5],
                "changes_summary": s.get("changes_summary", ""),
            }
            logger.info(
                "run_pipeline_async.previous_context_loaded",
                prev_run_id=prev.run_id,
                prev_score=previous_run_context["seo_score"],
                prev_changes=previous_run_context["changes_applied"],
            )

    auto_approved = run.approval_status == "approved"
    graph = await get_compiled_graph(skip_human_interrupt=auto_approved)
    initial_state = build_initial_state(
        run_id=run_id,
        thread_id=run.thread_id,
        site_id=run.site_id,
        target_url=run.target_url,
        mode=run.mode,
        approval_status="approved" if auto_approved else "pending",
        previous_run_context=previous_run_context,
        max_pages=run.max_pages,
    )

    config = {"configurable": {"thread_id": run.thread_id}}

    try:
        # Run until first interrupt (human_review) or completion
        result = await graph.ainvoke(initial_state, config=config)

        # Re-check DB status: the run may have been cancelled mid-execution via the API.
        async with AsyncSessionLocal() as _db:
            _run_check = await WorkflowService(_db).get_run(run_id)
        if _run_check and _run_check.status == "cancelled":
            logger.info("run_pipeline_async.cancelled_mid_run", run_id=run_id)
            return {"status": "cancelled"}

        # Check if we stopped at human_review (interrupt)
        graph_state = await graph.aget_state(config)
        next_nodes = graph_state.next

        if next_nodes and "human_review" in next_nodes:
            # Graph paused — create approval request
            tasks = result.get("execution_tasks", [])
            approval = None
            async with AsyncSessionLocal() as db:
                svc = WorkflowService(db)
                approval = await svc.mark_waiting_approval(run_id, tasks)
                await svc.emit_event(
                    run_id, "human_review", "approval_requested",
                    payload={"tasks_count": len(tasks)}
                )
                await db.commit()
            logger.info("run_pipeline_async.paused_for_approval", run_id=run_id, tasks=len(tasks))

            # Send email notification to all configured recipients
            try:
                from src.services.email_service import send_approval_email
                await send_approval_email(
                    run_id=run_id,
                    target_url=run.target_url,
                    plan_summary=approval.plan_summary if approval else {},
                    expires_at=approval.expires_at if approval else None,
                )
            except Exception as exc:
                # Email failure must never crash the pipeline
                logger.warning("run_pipeline_async.email_failed", run_id=run_id, error=str(exc))

            return {"status": "waiting_for_approval", "tasks_count": len(tasks)}
        else:
            # Completed in one shot (dry_run or auto-approved)
            summary = _build_summary(result)
            async with AsyncSessionLocal() as db:
                svc = WorkflowService(db)
                await svc.mark_completed(run_id, summary)
                await db.commit()
            return {"status": "completed", **summary}

    except Exception as exc:
        async with AsyncSessionLocal() as db:
            svc = WorkflowService(db)
            await svc.mark_failed(run_id, str(exc))
            await db.commit()
        raise


# ─── Task 2: resume_pipeline_task ────────────────────────────────────────────

@celery_app.task(
    bind=True,
    name="src.worker.tasks.resume_pipeline_task",
    max_retries=2,
    default_retry_delay=30,
    soft_time_limit=settings.worker_soft_time_limit,
    time_limit=settings.worker_hard_time_limit,
)
def resume_pipeline_task(self, run_id: str) -> dict[str, Any]:
    """Resume a paused pipeline after approval.

    Uses the same thread_id to restore LangGraph checkpoint from Postgres,
    injects the approval decision into the graph state, then continues.

    Args:
        run_id: The WorkflowRun.run_id that was waiting for approval.
    """
    try:
        return asyncio.run(_resume_pipeline_async(run_id))
    except Exception as exc:
        logger.error("resume_pipeline_task.failed", run_id=run_id, error=str(exc))
        asyncio.run(_mark_run_failed(run_id, str(exc)))
        raise self.retry(exc=exc)


async def _resume_pipeline_async(run_id: str) -> dict[str, Any]:
    from src.database.db import AsyncSessionLocal
    from src.services.workflow_service import WorkflowService
    from src.graph.seo_graph import get_compiled_graph

    async with AsyncSessionLocal() as db:
        svc = WorkflowService(db)
        run = await svc.get_run(run_id)
        if not run:
            raise ValueError(f"WorkflowRun {run_id} not found")
        if run.approval_status != "approved":
            raise ValueError(f"Run {run_id} is not approved (status: {run.approval_status})")
        await svc.mark_running(run_id, node="fix_executor")
        await db.commit()

    logger.info("resume_pipeline_async.start", run_id=run_id)

    if not settings.langgraph_checkpoint_url and run.mode == "preview":
        logger.warning(
            "resume_pipeline_async.no_checkpoint_fallback",
            run_id=run_id,
            note="Using ApprovalRequest execution_tasks because LANGGRAPH_CHECKPOINT_URL is unset.",
        )
        return await _resume_from_approval_tasks_async(run_id)

    graph = await get_compiled_graph()
    config = {"configurable": {"thread_id": run.thread_id}}

    # Inject approval into the checkpoint state before resuming
    await graph.aupdate_state(
        config=config,
        values={"approval_status": "approved"},
        as_node="human_review",
    )

    # Resume from checkpoint — pass None as input to continue from last state
    result = await graph.ainvoke(None, config=config)

    summary = _build_summary(result)
    async with AsyncSessionLocal() as db:
        svc = WorkflowService(db)
        await svc.mark_completed(run_id, summary)
        await svc.emit_event(run_id, "ranking_monitor", "node_complete", payload=summary)
        await db.commit()

    logger.info("resume_pipeline_async.complete", run_id=run_id)
    return {"status": "completed", **summary}


async def _resume_from_approval_tasks_async(run_id: str) -> dict[str, Any]:
    """Apply approved preview tasks without a LangGraph checkpoint.

    This is a local/dev fallback. Production should set LANGGRAPH_CHECKPOINT_URL
    so the full graph can resume with all upstream context.
    """
    from src.agents.fix_executor_agent import fix_executor_node
    from src.agents.validation_agent import validation_node
    from src.database.db import AsyncSessionLocal
    from src.services.workflow_service import WorkflowService

    async with AsyncSessionLocal() as db:
        svc = WorkflowService(db)
        run = await svc.get_run(run_id)
        approval = await svc.get_latest_approval(run_id, status="approved")
        if not run:
            raise ValueError(f"WorkflowRun {run_id} not found")
        if not approval:
            raise ValueError(f"Approved request for run {run_id} not found")

        plan = approval.plan_summary or {}
        tasks = plan.get("execution_tasks") or []
        if not tasks:
            raise ValueError(
                f"Approved request for run {run_id} does not include executable tasks. "
                "Set LANGGRAPH_CHECKPOINT_URL and rerun the pipeline."
            )
        run_data = {
            "site_id": run.site_id,
            "target_url": run.target_url,
            "mode": run.mode,
        }

    approved_tasks = [{**task, "approved": True} for task in tasks]
    state: dict[str, Any] = {
        "run_id": run_id,
        "site_id": run_data["site_id"],
        "target_url": run_data["target_url"],
        "mode": run_data["mode"],
        "approval_status": "approved",
        "execution_tasks": approved_tasks,
        "applied_changes": [],
        "failed_changes": [],
        "validation_results": [],
        "rollback_triggered": [],
        "errors": [],
        "messages": [],
    }

    execution_result = await fix_executor_node(state)  # type: ignore[arg-type]
    state.update(execution_result)
    if state.get("applied_changes"):
        validation_result = await validation_node(state)  # type: ignore[arg-type]
        state.update(validation_result)

    summary = _build_summary(state)
    async with AsyncSessionLocal() as db:
        svc = WorkflowService(db)
        await svc.mark_completed(run_id, summary)
        await svc.emit_event(run_id, "fix_executor", "node_complete", payload=summary)
        await db.commit()

    logger.info("resume_pipeline_async.fallback_complete", run_id=run_id)
    return {"status": "completed", **summary}


# ─── Task 3: ranking_monitor_task ────────────────────────────────────────────

@celery_app.task(
    bind=True,
    name="src.worker.tasks.ranking_monitor_task",
    max_retries=2,
    default_retry_delay=300,
    soft_time_limit=1800,
    time_limit=3600,
)
def ranking_monitor_task(self, site_id: int) -> dict[str, Any]:
    """Run a lightweight ranking monitor pass for a site.

    Does not re-crawl — uses cached page data from last crawl.
    Checks SERP positions and GSC data, generates a delta report.

    Args:
        site_id: The Site.id to monitor.
    """
    try:
        return asyncio.run(_ranking_monitor_async(site_id))
    except Exception as exc:
        logger.error("ranking_monitor_task.failed", site_id=site_id, error=str(exc))
        raise self.retry(exc=exc)


async def _ranking_monitor_async(site_id: int) -> dict[str, Any]:
    from src.database.db import AsyncSessionLocal
    from src.models.schemas import Page, Site
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Site).where(Site.id == site_id))
        site = result.scalar_one_or_none()
        if not site:
            raise ValueError(f"Site {site_id} not found")

        pages_result = await db.execute(
            select(Page).where(Page.site_id == site_id).limit(20)
        )
        pages = pages_result.scalars().all()

    if not pages:
        return {"site_id": site_id, "status": "no_pages", "rankings_checked": 0}

    from src.tools.serp_tools import check_serp_position
    from src.tools.serp_tools import get_search_console_data

    rankings_checked = 0
    for page in pages[:10]:
        if page.focus_keyword and page.url:
            await check_serp_position.ainvoke({
                "keyword": page.focus_keyword,
                "target_url": page.url,
            })
            rankings_checked += 1

    gsc = await get_search_console_data.ainvoke(
        {"site_url": site.base_url, "days": 7}
    )

    logger.info("ranking_monitor_async.complete", site_id=site_id, checked=rankings_checked)
    return {
        "site_id": site_id,
        "status": "completed",
        "rankings_checked": rankings_checked,
        "gsc_error": gsc.get("error"),
    }


# ─── Task 4: execute_approved_task ───────────────────────────────────────────

@celery_app.task(
    bind=True,
    name="src.worker.tasks.execute_approved_task",
    max_retries=1,
    default_retry_delay=120,
    soft_time_limit=settings.worker_soft_time_limit,
    time_limit=settings.worker_hard_time_limit,
)
def execute_approved_task(self, site_id: int) -> dict[str, Any]:
    """Find all approved-but-not-executed plans and resume them.

    Called by the weekly execution scheduler job.
    """
    try:
        return asyncio.run(_execute_approved_async(site_id))
    except Exception as exc:
        logger.error("execute_approved_task.failed", site_id=site_id, error=str(exc))
        raise self.retry(exc=exc)


async def _execute_approved_async(site_id: int) -> dict[str, Any]:
    from src.database.db import AsyncSessionLocal
    from src.services.workflow_service import WorkflowService
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        svc = WorkflowService(db)
        # Find all runs for this site that are approved and waiting
        runs = await svc.list_runs(site_id=site_id, status="waiting_for_approval")
        approved_runs = [r for r in runs if r.approval_status == "approved"]
        await db.commit()

    if not approved_runs:
        logger.info("execute_approved_async.no_runs", site_id=site_id)
        return {"site_id": site_id, "resumed": 0}

    resumed = 0
    for run in approved_runs:
        try:
            await _resume_pipeline_async(run.run_id)
            resumed += 1
        except Exception as exc:
            logger.error("execute_approved_async.resume_failed", run_id=run.run_id, error=str(exc))

    return {"site_id": site_id, "resumed": resumed}


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _build_summary(result: dict[str, Any]) -> dict[str, Any]:
    applied = result.get("applied_changes", [])
    issues = result.get("issues", [])
    change_types = list({c["change_type"] for c in applied})
    changed_pages = list({c["page_url"] for c in applied})
    changes_summary = (
        f"Last run applied {len(applied)} changes across {len(changed_pages)} pages; "
        f"top change types: {', '.join(change_types[:_SUMMARY_MAX_TYPES]) or 'none'}."
    )

    # Aggregate PSI / Core Web Vitals from audit results
    audit_results = result.get("audit_results", [])
    psi_data = [r["psi"] for r in audit_results if r.get("psi")]
    cwv: dict[str, Any] = {}
    if psi_data:
        def _avg(key: str) -> float | None:
            vals = [p[key] for p in psi_data if p.get(key) is not None]
            return round(sum(vals) / len(vals), 1) if vals else None

        def _avg_ms(key: str) -> float | None:
            vals = [p[key] for p in psi_data if p.get(key) is not None]
            return round(sum(vals) / len(vals)) if vals else None

        cwv = {
            "pages_tested": len(psi_data),
            "avg_performance_score": _avg("performance_score"),
            "avg_accessibility_score": _avg("accessibility_score"),
            "avg_seo_score": _avg("seo_score"),
            "avg_lcp_ms": _avg_ms("lcp"),
            "avg_cls": round(sum(p["cls"] for p in psi_data if p.get("cls") is not None)
                             / max(1, sum(1 for p in psi_data if p.get("cls") is not None)), 3)
                        if any(p.get("cls") is not None for p in psi_data) else None,
            "avg_fcp_ms": _avg_ms("fcp"),
            "avg_ttfb_ms": _avg_ms("ttfb"),
        }

    issues_sorted = sorted(issues, key=lambda x: x.get("impact_score", 0), reverse=True)

    return {
        # ── Key metrics ───────────────────────────────────────────────────────
        "overall_seo_score": result.get("overall_seo_score", 0),
        "issues_found": len(issues),
        "changes_applied": len(applied),
        "changes_failed": len(result.get("failed_changes", [])),
        "rollbacks": len(result.get("rollback_triggered", [])),
        "errors": result.get("errors", []),
        "changes_summary": changes_summary,
        # ── Crawl ─────────────────────────────────────────────────────────────
        "crawl_stats": result.get("crawl_stats", {}),
        # ── All issues (full detail, sorted by impact) ────────────────────────
        "all_issues": [
            {
                "rule_id": i["rule_id"],
                "category": i.get("category", ""),
                "severity": i.get("severity", ""),
                "title": i.get("title", ""),
                "description": i.get("description", ""),
                "current_value": i.get("current_value", ""),
                "suggested_fix": i.get("suggested_fix", ""),
                "impact_score": i.get("impact_score", 0),
                "page_url": i.get("page_url", ""),
            }
            for i in issues_sorted
        ],
        # kept for backwards-compat (previous_run_context reader)
        "top_issues": [
            {"rule_id": i["rule_id"], "severity": i["severity"], "page_url": i["page_url"]}
            for i in issues_sorted[:_SUMMARY_MAX_ISSUES]
        ],
        # ── Keywords & gaps ───────────────────────────────────────────────────
        "keyword_suggestions": result.get("keyword_suggestions", []),
        "content_gaps": result.get("content_gaps", []),
        "keywords_targeted": [
            k["keyword"] for k in result.get("keyword_suggestions", [])[:5]
        ],
        # ── Content suggestions ───────────────────────────────────────────────
        "content_suggestions": [
            {
                "page_url": s.get("page_url", ""),
                "suggestion_type": s.get("suggestion_type", ""),
                "field_name": s.get("field_name", ""),
                "old_value": s.get("old_value", ""),
                "new_value": s.get("new_value", ""),
                "rationale": s.get("rationale", ""),
                "risk_level": s.get("risk_level", ""),
                "impact_score": s.get("impact_score", 0),
            }
            for s in result.get("content_suggestions", [])
        ],
        # ── Execution tasks ───────────────────────────────────────────────────
        "execution_tasks": [
            {
                "task_id": t.get("task_id", ""),
                "page_url": t.get("page_url", ""),
                "change_type": t.get("change_type", ""),
                "field_name": t.get("field_name", ""),
                "old_value": t.get("old_value", ""),
                "new_value": t.get("new_value", ""),
                "risk_level": t.get("risk_level", ""),
                "applied": t.get("applied", False),
                "error": t.get("error"),
            }
            for t in result.get("execution_tasks", [])
        ],
        # ── Per-page audit summaries ──────────────────────────────────────────
        "page_audit_summaries": [
            {
                "url": r.get("url", r.get("page_url", "")),
                "seo_score": r.get("seo_score"),
                "issues_count": r.get("issues_count", 0),
                "title": r.get("title", ""),
                "word_count": r.get("word_count", 0),
                "load_time_ms": r.get("load_time_ms", 0),
                "psi": r.get("psi", {}),
            }
            for r in audit_results
        ],
        # ── Rankings & competitive ────────────────────────────────────────────
        "ranking_data": result.get("ranking_data", []),
        "competitive_insights": result.get("competitive_insights", []),
        # ── Validation ────────────────────────────────────────────────────────
        "validation_results": result.get("validation_results", []),
        # ── Performance ───────────────────────────────────────────────────────
        "performance_report": result.get("performance_report", {}),
        "core_web_vitals": cwv,
        # ── Backwards-compat fields ───────────────────────────────────────────
        "applied_change_types": change_types,
        "pages_changed": changed_pages,
    }


async def _mark_run_failed(run_id: str, error: str) -> None:
    try:
        from src.database.db import AsyncSessionLocal
        from src.services.workflow_service import WorkflowService

        async with AsyncSessionLocal() as db:
            svc = WorkflowService(db)
            await svc.mark_failed(run_id, error)
            await db.commit()
    except Exception:
        pass  # Best effort
