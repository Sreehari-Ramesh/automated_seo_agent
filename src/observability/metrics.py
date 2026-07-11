"""Observability layer — node timing, success/failure tracking, rollback metrics.

Two integration points:

1. `track_node(name)` — async decorator for LangGraph node functions.
   Wraps nodes to record start, completion, and error events.

2. `NodeTimer` — context manager for manual timing of sub-steps.

3. `record_node_event` — explicit event writer for use inside tasks.

4. `get_node_stats` — query aggregated metrics from NodeMetric table.
"""

from __future__ import annotations

import functools
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime
from typing import Any, AsyncGenerator, Callable

import structlog

logger = structlog.get_logger(__name__)


# ── Per-node token accumulator (async-safe via ContextVar) ────────────────────

_node_tokens: ContextVar[dict[str, int]] = ContextVar(
    "_node_tokens", default={"input": 0, "output": 0, "calls": 0}
)


def reset_token_usage() -> None:
    """Call at the start of each node to get a fresh counter."""
    _node_tokens.set({"input": 0, "output": 0, "calls": 0})


def accumulate_token_usage(response: Any) -> None:
    """Extract usage_metadata from a LangChain AIMessage and add to the running total.

    Call this after every _llm.ainvoke / _llm_with_tools.ainvoke call.
    """
    usage = getattr(response, "usage_metadata", None)
    if not usage:
        return
    current = _node_tokens.get()
    _node_tokens.set({
        "input":  current["input"]  + usage.get("input_tokens", 0),
        "output": current["output"] + usage.get("output_tokens", 0),
        "calls":  current["calls"]  + 1,
    })


def get_token_usage() -> dict[str, int]:
    """Return the accumulated token counts for the current node."""
    return dict(_node_tokens.get())


# ── Decorator ─────────────────────────────────────────────────────────────────

def track_node(node_name: str) -> Callable:
    """Async decorator that instruments a LangGraph node function.

    Automatically:
    - Records node_start event with run_id from state
    - Records duration_ms on completion
    - Records error event with exception info on failure
    - Updates NodeMetric aggregates (non-blocking, best effort)
    - Updates WorkflowRun.current_node
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(state: dict, *args, **kwargs) -> dict:
            run_id = state.get("run_id", "unknown")
            start = time.monotonic()
            reset_token_usage()

            # Emit node_start
            await _emit_safe(run_id, node_name, "node_start", {"mode": state.get("mode")})
            await _update_current_node_safe(run_id, node_name)

            try:
                result = await func(state, *args, **kwargs)
                duration_ms = int((time.monotonic() - start) * 1000)
                tokens = get_token_usage()

                logger.info(
                    "node.tokens",
                    node=node_name,
                    run_id=run_id,
                    input_tokens=tokens["input"],
                    output_tokens=tokens["output"],
                    total_tokens=tokens["input"] + tokens["output"],
                    llm_calls=tokens["calls"],
                    duration_ms=duration_ms,
                )

                # Emit node_complete
                await _emit_safe(
                    run_id,
                    node_name,
                    "node_complete",
                    {
                        "duration_ms": duration_ms,
                        "errors_added": len(result.get("errors", [])),
                        "tokens": tokens,
                    },
                    duration_ms=duration_ms,
                )
                await _update_node_metric_safe(node_name, success=True, duration_ms=duration_ms)

                logger.debug(
                    "node.complete",
                    node=node_name,
                    run_id=run_id,
                    duration_ms=duration_ms,
                )
                return result

            except Exception as exc:
                duration_ms = int((time.monotonic() - start) * 1000)
                tokens = get_token_usage()
                await _emit_safe(
                    run_id,
                    node_name,
                    "node_error",
                    {"error": str(exc), "duration_ms": duration_ms, "tokens": tokens},
                    duration_ms=duration_ms,
                )
                await _update_node_metric_safe(node_name, success=False, duration_ms=duration_ms)
                logger.error(
                    "node.error",
                    node=node_name,
                    run_id=run_id,
                    error=str(exc),
                    duration_ms=duration_ms,
                )
                raise

        return wrapper
    return decorator


# ── Context manager ───────────────────────────────────────────────────────────

class NodeTimer:
    """Manual timing context manager for sub-steps within a node."""

    def __init__(self, label: str) -> None:
        self.label = label
        self._start: float = 0.0
        self.duration_ms: int = 0

    async def __aenter__(self) -> "NodeTimer":
        self._start = time.monotonic()
        return self

    async def __aexit__(self, *_) -> None:
        self.duration_ms = int((time.monotonic() - self._start) * 1000)
        logger.debug("node_timer", label=self.label, duration_ms=self.duration_ms)


# ── Event writer ──────────────────────────────────────────────────────────────

async def record_node_event(
    run_id: str,
    node_name: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
    duration_ms: int | None = None,
) -> None:
    """Write a WorkflowEvent record from a Celery task or manual call."""
    await _emit_safe(run_id, node_name, event_type, payload or {}, duration_ms=duration_ms)


# ── Stats query ───────────────────────────────────────────────────────────────

async def get_node_stats() -> list[dict[str, Any]]:
    """Return aggregated NodeMetric rows for the dashboard."""
    try:
        from src.database.db import AsyncSessionLocal
        from src.models.workflow import NodeMetric
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(NodeMetric).order_by(NodeMetric.node_name.asc())
            )
            metrics = result.scalars().all()
            return [
                {
                    "node_name": m.node_name,
                    "total_runs": m.total_runs,
                    "success_rate": round(
                        m.success_count / m.total_runs * 100 if m.total_runs else 0, 1
                    ),
                    "failure_count": m.failure_count,
                    "avg_duration_ms": round(m.avg_duration_ms),
                    "max_duration_ms": m.max_duration_ms,
                    "last_run_at": m.last_run_at.isoformat() if m.last_run_at else None,
                }
                for m in metrics
            ]
    except Exception:
        return []


# ── Internal helpers ──────────────────────────────────────────────────────────

async def _emit_safe(
    run_id: str,
    node_name: str,
    event_type: str,
    payload: dict[str, Any],
    duration_ms: int | None = None,
) -> None:
    """Write a WorkflowEvent — silently fails to not block the pipeline."""
    if run_id == "unknown":
        return
    try:
        from src.database.db import AsyncSessionLocal
        from src.models.workflow import WorkflowEvent

        async with AsyncSessionLocal() as db:
            event = WorkflowEvent(
                run_id=run_id,
                node_name=node_name,
                event_type=event_type,
                payload=payload,
                duration_ms=duration_ms,
            )
            db.add(event)
            await db.commit()
    except Exception as exc:
        logger.warning("metrics.emit_failed", node=node_name, error=str(exc))


async def _update_current_node_safe(run_id: str, node_name: str) -> None:
    if run_id == "unknown":
        return
    try:
        from src.database.db import AsyncSessionLocal
        from src.services.workflow_service import WorkflowService

        async with AsyncSessionLocal() as db:
            svc = WorkflowService(db)
            await svc.update_node(run_id, node_name)
            await db.commit()
    except Exception:
        pass


async def _update_node_metric_safe(
    node_name: str, success: bool, duration_ms: int
) -> None:
    """Upsert NodeMetric row for this node — best effort."""
    try:
        from src.database.db import AsyncSessionLocal
        from src.models.workflow import NodeMetric
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(NodeMetric).where(NodeMetric.node_name == node_name)
            )
            metric = result.scalar_one_or_none()
            if not metric:
                metric = NodeMetric(
                    node_name=node_name,
                    total_runs=0,
                    success_count=0,
                    failure_count=0,
                    avg_duration_ms=0.0,
                    max_duration_ms=0,
                )
                db.add(metric)

            # Running average
            old_total = metric.total_runs
            metric.total_runs += 1
            if success:
                metric.success_count += 1
            else:
                metric.failure_count += 1

            # Update moving average
            metric.avg_duration_ms = (
                (metric.avg_duration_ms * old_total + duration_ms) / metric.total_runs
            )
            if duration_ms > metric.max_duration_ms:
                metric.max_duration_ms = duration_ms
            metric.last_run_at = datetime.utcnow()
            await db.commit()
    except Exception as exc:
        logger.warning("metrics.update_metric_failed", node=node_name, error=str(exc))
