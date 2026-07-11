"""Scheduler — three separate jobs, each enqueuing a Celery task.

Jobs do NOT run pipeline logic directly.
They only enqueue Celery tasks → worker executes.

Job 1: audit_job      — month-end 2 AM   → enqueue run_pipeline_task per active site
Job 2: execution_job  — Mon+Thu 3 AM → enqueue execute_approved_task per active site
Job 3: ranking_job    — every 6 hrs  → enqueue ranking_monitor_task per active site
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from src.config import get_settings

settings = get_settings()
logger = structlog.get_logger(__name__)

_scheduler: AsyncIOScheduler | None = None


def _is_last_day_of_month(day: date | None = None) -> bool:
    """Return True when `day` is the calendar month-end date."""
    current = day or datetime.now().date()
    return (current + timedelta(days=1)).month != current.month


# ── Site loader ───────────────────────────────────────────────────────────────

async def _get_active_sites() -> list[dict]:
    """Return all active Site rows. Falls back to env var for single-site setups."""
    try:
        from src.database.db import AsyncSessionLocal
        from src.models.schemas import Site
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Site).where(Site.is_active == True))
            sites = result.scalars().all()
            if sites:
                return [{"site_id": s.id, "url": s.base_url} for s in sites]
    except Exception as exc:
        logger.warning("scheduler.db_site_lookup_failed", error=str(exc))

    # Fallback: single site from env
    if settings.wp_base_url:
        return [{"site_id": 1, "url": settings.wp_base_url}]
    return []


# ── Job 1: Audit + Planning ───────────────────────────────────────────────────

async def _audit_job():
    """Month-end: create a WorkflowRun + enqueue run_pipeline_task for each site.

    This job ONLY crawls, audits, plans, and generates content suggestions.
    It DOES NOT apply any changes (mode stays at configured level).
    The pipeline pauses at human_review — no auto-execution.
    """
    if not _is_last_day_of_month():
        logger.info("scheduler.audit_job.skip_not_month_end")
        return

    sites = await _get_active_sites()
    if not sites:
        logger.warning("scheduler.audit_job.no_sites")
        return

    for site in sites:
        logger.info("scheduler.audit_job.enqueue", site_id=site["site_id"], url=site["url"])
        try:
            # Create WorkflowRun in DB first
            from src.database.db import AsyncSessionLocal
            from src.services.workflow_service import WorkflowService

            async with AsyncSessionLocal() as db:
                svc = WorkflowService(db)
                run = await svc.create_run(
                    site_id=site["site_id"],
                    target_url=site["url"],
                    mode=settings.execution_mode,
                    max_pages=0,  # scheduled runs always crawl the full site
                )
                await db.commit()
                run_id = run.run_id

            # Enqueue Celery task
            from src.worker.tasks import run_pipeline_task
            run_pipeline_task.apply_async(
                args=[run_id],
                queue=settings.pipeline_queue,
            )
            logger.info("scheduler.audit_job.enqueued", run_id=run_id, site=site["url"])

        except Exception as exc:
            logger.error("scheduler.audit_job.error", site=site["url"], error=str(exc))


# ── Job 2: Execute Approved Plans ────────────────────────────────────────────

async def _execution_job():
    """Mon + Thu 3 AM: enqueue execute_approved_task for each active site.

    This finds all approved-and-waiting WorkflowRuns and resumes them.
    Only runs that a human has approved will be executed.
    """
    sites = await _get_active_sites()
    if not sites:
        logger.warning("scheduler.execution_job.no_sites")
        return

    for site in sites:
        logger.info("scheduler.execution_job.enqueue", site_id=site["site_id"])
        try:
            from src.worker.tasks import execute_approved_task
            execute_approved_task.apply_async(
                args=[site["site_id"]],
                queue=settings.pipeline_queue,
            )
        except Exception as exc:
            logger.error("scheduler.execution_job.error", site=site["url"], error=str(exc))


# ── Job 3: Ranking Monitor ────────────────────────────────────────────────────

async def _ranking_job():
    """Every 6 hours: enqueue ranking_monitor_task per active site.

    Checks SERP positions and GSC data without triggering any edits.
    """
    sites = await _get_active_sites()
    if not sites:
        return

    for site in sites:
        try:
            from src.worker.tasks import ranking_monitor_task
            ranking_monitor_task.apply_async(
                args=[site["site_id"]],
                queue=settings.monitor_queue,
            )
            logger.info("scheduler.ranking_job.enqueued", site_id=site["site_id"])
        except Exception as exc:
            logger.error("scheduler.ranking_job.error", site=site["url"], error=str(exc))


# ── Scheduler lifecycle ───────────────────────────────────────────────────────

def start_scheduler() -> AsyncIOScheduler:
    """Create and start the APScheduler with all three production jobs."""
    global _scheduler
    if _scheduler and _scheduler.running:
        return _scheduler

    _scheduler = AsyncIOScheduler()

    # Job 1 — Audit + planning (month-end only)
    _scheduler.add_job(
        _audit_job,
        CronTrigger.from_crontab(settings.audit_cron),
        id="audit_planning",
        name="Month-End Audit + Planning (Crawl → Audit → Plan, no writes)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Job 2 — Execute approved plans only
    _scheduler.add_job(
        _execution_job,
        CronTrigger.from_crontab(settings.execution_cron),
        id="execute_approved",
        name="Execute Approved Plans (Mon+Thu 3 AM)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Job 3 — Ranking monitoring
    _scheduler.add_job(
        _ranking_job,
        CronTrigger.from_crontab(settings.ranking_cron),
        id="ranking_monitor",
        name="Ranking Monitor (every 6h)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    _scheduler.start()
    logger.info(
        "scheduler.started",
        jobs=[j.name for j in _scheduler.get_jobs()],
        audit_cron=settings.audit_cron,
        execution_cron=settings.execution_cron,
        ranking_cron=settings.ranking_cron,
    )
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("scheduler.stopped")

