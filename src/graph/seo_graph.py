"""LangGraph orchestration — production-grade with:

- PostgreSQL-backed checkpoint persistence (AsyncPostgresSaver)
- Policy gate node between content generation and human review
- True interrupt at human_review for pause/resume approval flow
- Singleton compiled graph with shared connection pool
- Observability decorators on all nodes

Graph topology:
  START → crawler → seo_audit → keyword_planner → content_generator
       → policy_gate → INTERRUPT(human_review) → fix_executor → validator
       → ranking_monitor → END
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Literal

import structlog
from langgraph.graph import END, START, StateGraph

from src.agents.crawler_agent import crawler_node
from src.agents.content_generator_agent import content_generator_node
from src.agents.fix_executor_agent import fix_executor_node
from src.agents.keyword_planner_agent import keyword_planner_node
from src.agents.policy_gate_agent import policy_gate_node
from src.agents.ranking_monitor_agent import ranking_monitor_node
from src.agents.seo_audit_agent import seo_audit_node
from src.agents.serp_analysis_agent import serp_analysis_node
from src.agents.state import SEOAgentState
from src.agents.validation_agent import validation_node
from src.config import get_settings
from src.observability.metrics import track_node

settings = get_settings()
logger = structlog.get_logger(__name__)

# ── Instrumented node wrappers ────────────────────────────────────────────────

_crawler = track_node("crawler")(crawler_node)
_seo_audit = track_node("seo_audit")(seo_audit_node)
_keyword_planner = track_node("keyword_planner")(keyword_planner_node)
_serp_analysis = track_node("serp_analysis")(serp_analysis_node)
_content_generator = track_node("content_generator")(content_generator_node)
_policy_gate = track_node("policy_gate")(policy_gate_node)
_fix_executor = track_node("fix_executor")(fix_executor_node)
_validator = track_node("validator")(validation_node)
_ranking_monitor = track_node("ranking_monitor")(ranking_monitor_node)


# ── Human-in-the-loop approval node ──────────────────────────────────────────

async def human_review_node(state: SEOAgentState) -> dict:
    """Pass-through node placed AFTER the interrupt.

    LangGraph interrupts BEFORE this node. When the worker resumes,
    it updates approval_status in the checkpoint state, then this node
    marks all tasks approved and passes them to fix_executor.
    """
    approval_status = state.get("approval_status", "pending")
    tasks = state.get("execution_tasks", [])
    if approval_status == "approved":
        tasks = [{**t, "approved": True} for t in tasks]
    return {"execution_tasks": tasks, "approval_status": approval_status}


# ── Conditional routing ───────────────────────────────────────────────────────

def route_after_crawl(state: SEOAgentState) -> Literal["seo_audit", "ranking_monitor"]:
    return "seo_audit" if state.get("crawled_pages") else "ranking_monitor"


def route_after_audit(state: SEOAgentState) -> Literal["keyword_planner", "ranking_monitor"]:
    critical_high = [
        i for i in state.get("issues", []) if i["severity"] in ("critical", "high")
    ]
    return "keyword_planner" if critical_high else "ranking_monitor"


def route_after_review(
    state: SEOAgentState,
) -> Literal["fix_executor", "ranking_monitor"]:
    return "fix_executor" if state.get("approval_status") == "approved" else "ranking_monitor"


def route_after_execution(
    state: SEOAgentState,
) -> Literal["validator", "ranking_monitor"]:
    applied = state.get("applied_changes", [])
    return "validator" if applied and state.get("mode") in ("preview", "live") else "ranking_monitor"


# ── Graph builder ─────────────────────────────────────────────────────────────

def _build_raw_graph() -> StateGraph:
    graph = StateGraph(SEOAgentState)

    graph.add_node("crawler", _crawler)
    graph.add_node("seo_audit", _seo_audit)
    graph.add_node("keyword_planner", _keyword_planner)
    graph.add_node("serp_analysis", _serp_analysis)
    graph.add_node("content_generator", _content_generator)
    graph.add_node("policy_gate", _policy_gate)
    graph.add_node("human_review", human_review_node)
    graph.add_node("fix_executor", _fix_executor)
    graph.add_node("validator", _validator)
    graph.add_node("ranking_monitor", _ranking_monitor)

    graph.add_edge(START, "crawler")
    graph.add_conditional_edges("crawler", route_after_crawl,
        {"seo_audit": "seo_audit", "ranking_monitor": "ranking_monitor"})
    graph.add_conditional_edges("seo_audit", route_after_audit,
        {"keyword_planner": "keyword_planner", "ranking_monitor": "ranking_monitor"})
    graph.add_edge("keyword_planner", "serp_analysis")
    graph.add_edge("serp_analysis", "content_generator")
    graph.add_edge("content_generator", "policy_gate")
    graph.add_edge("policy_gate", "human_review")
    graph.add_conditional_edges("human_review", route_after_review,
        {"fix_executor": "fix_executor", "ranking_monitor": "ranking_monitor"})
    graph.add_conditional_edges("fix_executor", route_after_execution,
        {"validator": "validator", "ranking_monitor": "ranking_monitor"})
    graph.add_edge("validator", "ranking_monitor")
    graph.add_edge("ranking_monitor", END)

    return graph


# ── Singleton compiled graph with PostgreSQL checkpoint ───────────────────────

_graph_lock = asyncio.Lock()
_compiled_graph = None  # type: ignore[assignment]


async def get_compiled_graph(skip_human_interrupt: bool = False):
    """Return singleton compiled graph. Initialises PostgresSaver on first call.

    Falls back to MemorySaver when LANGGRAPH_CHECKPOINT_URL is unset (local dev).
    interrupt_before=["human_review"] creates the true pause-for-approval point.

    Args:
        skip_human_interrupt: When True (e.g. run_single_page.py with pre-approved
            state) the interrupt is omitted so the graph runs end-to-end in one call.
            The production default is False.
    """
    global _compiled_graph
    # Only use the singleton when running with the interrupt (production default).
    # skip_human_interrupt runs are always compiled fresh (test / single-page use).
    if not skip_human_interrupt and _compiled_graph is not None:
        return _compiled_graph

    async with _graph_lock:
        if not skip_human_interrupt and _compiled_graph is not None:
            return _compiled_graph

        raw = _build_raw_graph()

        if settings.langgraph_checkpoint_url:
            checkpointer = await _create_pg_checkpointer()
            logger.info("graph.checkpointer", backend="postgres")
        else:
            from langgraph.checkpoint.memory import MemorySaver
            checkpointer = MemorySaver()
            logger.warning(
                "graph.checkpointer",
                backend="memory",
                note="Set LANGGRAPH_CHECKPOINT_URL for durable state across restarts",
            )

        compiled = raw.compile(
            checkpointer=checkpointer,
            interrupt_before=[] if skip_human_interrupt else ["human_review"],
        )

        if not skip_human_interrupt:
            _compiled_graph = compiled

    return compiled


async def _create_pg_checkpointer():
    """Build a pooled AsyncPostgresSaver for LangGraph checkpoints."""
    try:
        from psycopg_pool import AsyncConnectionPool
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    except ImportError as exc:
        raise ImportError(
            "Install: pip install langgraph-checkpoint-postgres 'psycopg[binary,pool]'"
        ) from exc

    pool = AsyncConnectionPool(
        conninfo=settings.langgraph_checkpoint_url,
        max_size=10,
        open=False,
    )
    await pool.open()
    saver = AsyncPostgresSaver(pool)
    await saver.setup()   # Creates langgraph_checkpoints table if not exists
    return saver


# ── Initial state builder ─────────────────────────────────────────────────────

def build_initial_state(
    run_id: str,
    thread_id: str,
    site_id: int,
    target_url: str,
    mode: str,
    approval_status: str = "pending",
    previous_run_context: dict[str, Any] | None = None,
    max_pages: int | None = None,
) -> SEOAgentState:
    return SEOAgentState(
        run_id=run_id,
        site_id=site_id,
        target_url=target_url,
        mode=mode,  # type: ignore[typeddict-item]
        max_pages=max_pages,
        messages=[],
        crawled_pages=[],
        crawl_stats={},
        audit_results=[],
        issues=[],
        overall_seo_score=0.0,
        keyword_suggestions=[],
        content_gaps=[],
        competitive_insights=[],
        content_suggestions=[],
        execution_tasks=[],
        approval_status=approval_status,  # type: ignore[typeddict-item]
        approval_notes="",
        plan_id=None,
        applied_changes=[],
        failed_changes=[],
        validation_results=[],
        rollback_triggered=[],
        ranking_data=[],
        performance_report={},
        previous_run_context=previous_run_context or {},
        errors=[],
        next_node="",
    )


# ── CLI / direct entry point ──────────────────────────────────────────────────

async def run_seo_pipeline(
    target_url: str,
    site_id: int = 1,
    mode: str | None = None,
    approval_status: str = "pending",
    run_id: str | None = None,
) -> dict[str, Any]:
    """Run the pipeline directly (CLI, tests). Production should use Celery tasks."""
    _run_id = run_id or str(uuid.uuid4())
    _mode = mode or settings.execution_mode
    graph = await get_compiled_graph()
    initial = build_initial_state(
        run_id=_run_id, thread_id=_run_id, site_id=site_id,
        target_url=target_url, mode=_mode, approval_status=approval_status,
    )
    config = {"configurable": {"thread_id": _run_id}}
    logger.info("seo_pipeline.start", run_id=_run_id, url=target_url, mode=_mode)
    result = await graph.ainvoke(initial, config=config)
    logger.info("seo_pipeline.complete", run_id=_run_id,
                issues=len(result.get("issues", [])),
                applied=len(result.get("applied_changes", [])))
    return result
