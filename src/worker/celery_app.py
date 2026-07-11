"""Celery application — broker: Redis, backend: Redis."""

from __future__ import annotations

import asyncio

from celery import Celery
from celery.signals import worker_ready
from kombu import Exchange, Queue

from src.config import get_settings

settings = get_settings()

celery_app = Celery(
    "seo_agent",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["src.worker.tasks"],
)

celery_app.conf.update(
    # Serialization
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,

    # Reliability
    task_acks_late=True,                     # Ack after task completes, not on pickup
    task_reject_on_worker_lost=True,         # Reject (re-queue) if worker dies mid-task
    worker_prefetch_multiplier=1,            # One task at a time per worker thread

    # Timeouts
    task_soft_time_limit=settings.worker_soft_time_limit,
    task_time_limit=settings.worker_hard_time_limit,

    # Result TTL (24 hours)
    result_expires=86400,

    # Routing — two queues: pipeline (heavy, long-running) and monitor (lightweight)
    task_queues=(
        Queue(settings.pipeline_queue, Exchange(settings.pipeline_queue), routing_key=settings.pipeline_queue),
        Queue(settings.monitor_queue, Exchange(settings.monitor_queue), routing_key=settings.monitor_queue),
    ),
    task_default_queue=settings.pipeline_queue,
    task_routes={
        "src.worker.tasks.run_pipeline_task":    {"queue": settings.pipeline_queue},
        "src.worker.tasks.resume_pipeline_task": {"queue": settings.pipeline_queue},
        "src.worker.tasks.ranking_monitor_task": {"queue": settings.monitor_queue},
        "src.worker.tasks.execute_approved_task": {"queue": settings.pipeline_queue},
    },

    # Retry defaults
    task_max_retries=3,
    task_default_retry_delay=60,   # 60 seconds between retries
)


@worker_ready.connect
def _init_worker_db_schema(**_: object) -> None:
    """Ensure DB schema exists when workers start.

    This keeps SQLite fallback resilient when worker boot happens before API boot.
    """
    from src.database.db import init_db

    asyncio.run(init_db())
