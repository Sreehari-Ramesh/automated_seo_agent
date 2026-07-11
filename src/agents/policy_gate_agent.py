"""Policy Gate Agent — node that enforces SEO change policy before execution.

Sits between ContentGenerator and HumanReview in the graph.
Filters tasks through PolicyEngine and annotates the state with decisions.
"""

from __future__ import annotations

import json
from typing import Any

import structlog
from langchain_anthropic import ChatAnthropic  # noqa: F401  # production provider
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage

from src.agents.state import ExecutionTask, SEOAgentState
from src.config import get_settings
from src.database.db import AsyncSessionLocal
from src.services.policy_engine import PolicyEngine

settings = get_settings()
logger = structlog.get_logger(__name__)


# ── LLM — switch comment blocks to swap provider ──────────────────────────────
# Production (Claude):
_llm = ChatAnthropic(
    model=settings.claude_model,
    temperature=0.4,
    max_tokens=1024,
    anthropic_api_key=settings.anthropic_api_key,
)
# Local testing (OpenAI):
# _llm = ChatOpenAI(
#     model=settings.openai_model,
#     temperature=0.4,
#     max_tokens=settings.openai_max_tokens,
#     api_key=settings.openai_api_key,
# )

_SYSTEM_PROMPT = SystemMessage(content="""You are the Policy Gate Agent for an SEO system.
Your role is to review which changes pass through the anti-churn policy engine,
explain why any were blocked, and summarize what changes are cleared for approval.
Be concise and factual.""")


async def policy_gate_node(state: SEOAgentState) -> dict:
    """LangGraph node: evaluate all execution tasks through the PolicyEngine.

    Tasks that pass → kept in execution_tasks (marked allowed=True).
    Tasks that are blocked → removed from execution_tasks, logged in errors.
    Tasks that require manual approval → flagged in the task dict.
    """
    tasks = state.get("execution_tasks", [])
    errors: list[str] = list(state.get("errors", []))

    if not tasks:
        return {
            "execution_tasks": [],
            "errors": errors,
        }

    logger.info("policy_gate_agent.start", tasks=len(tasks))

    # ── Run PolicyEngine against the DB ──────────────────────────────────────
    async with AsyncSessionLocal() as db:
        engine = PolicyEngine(db)
        report = await engine.evaluate_batch(tasks)
        await db.commit()

    # ── Rebuild task list: allowed + manual_only only ─────────────────────────
    passed_task_ids = {t["task_id"] for t in report.allowed}
    manual_task_ids = {t["task_id"] for t in report.manual_only}

    filtered_tasks: list[ExecutionTask] = []
    for task in tasks:
        tid = task["task_id"]
        if tid in passed_task_ids:
            filtered_tasks.append(task)
        elif tid in manual_task_ids:
            # Flag this task as requiring manual approval
            filtered_tasks.append({**task, "risk_level": "high"})
        else:
            # Blocked — find the reason
            for blocked_task, decision in report.blocked:
                if blocked_task["task_id"] == tid:
                    errors.append(
                        f"[POLICY_BLOCKED] {task['change_type']} on {task['page_url']}: "
                        f"{decision.reason}"
                    )
                    break

    # ── LLM summary ───────────────────────────────────────────────────────────
    summary_prompt = (
        f"Policy gate results:\n"
        f"  Total tasks evaluated: {len(tasks)}\n"
        f"  Allowed (auto-execute eligible): {len(report.allowed)}\n"
        f"  Require manual approval: {len(report.manual_only)}\n"
        f"  Blocked: {len(report.blocked)}\n\n"
    )
    if report.blocked:
        summary_prompt += "Blocked tasks:\n" + "\n".join(
            f"  - {d.reason}" for _, d in report.blocked[:5]
        )

    llm_response = await _llm.ainvoke(
        [_SYSTEM_PROMPT, HumanMessage(content=summary_prompt)]
    )
    ai_summary = llm_response.content if hasattr(llm_response, "content") else str(llm_response)

    logger.info(
        "policy_gate_agent.complete",
        passed=len(report.allowed),
        manual_only=len(report.manual_only),
        blocked=len(report.blocked),
    )

    print(f"################################\nPolicy Gate complete. {len(report.allowed)} tasks allowed, {len(report.manual_only)} require manual approval, {len(report.blocked)} blocked. AI summary of the decisions: {ai_summary} ################################")

    return {
        "execution_tasks": filtered_tasks,
        "errors": errors,
        "messages": [
            HumanMessage(
                content=f"Policy Gate: {len(filtered_tasks)} tasks cleared. "
                f"{len(report.blocked)} blocked. {len(report.manual_only)} require manual approval.\n"
                f"{ai_summary}"
            )
        ],
    }
