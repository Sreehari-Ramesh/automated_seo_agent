"""Validation Agent — verifies applied changes persisted correctly; triggers rollback on failure."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import structlog
from bs4 import BeautifulSoup
from langchain_anthropic import ChatAnthropic  # noqa: F401  # production provider
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage

from src.agents.state import ExecutionTask, SEOAgentState
from src.config import get_settings
from src.tools.wordpress_tools import wp_restore_snapshot

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

_SYSTEM_PROMPT = SystemMessage(content="""You are the Validation Agent for an SEO system.
Your job is to verify that applied SEO changes are correctly reflected on the live site.
For each applied change, check:
1. The page is still accessible (200 OK)
2. The change is visible in the page source
3. No unintended side effects (broken layout indicators, error messages)
Report PASS or FAIL with specific details for each check.""")


async def _validate_task(task: ExecutionTask) -> dict[str, Any]:
    """Check that a single task's change is live on the page."""
    url = task["page_url"]
    change_type = task["change_type"]
    expected_value = task["new_value"]

    result: dict[str, Any] = {
        "task_id": task["task_id"],
        "url": url,
        "change_type": change_type,
        "passed": False,
        "checks": {},
        "notes": "",
    }

    # Skip new pages — they're created as drafts
    if change_type == "new_page":
        result["passed"] = True
        result["notes"] = "New page created as draft — no live validation needed"
        return result

    # Skip dry run tasks
    if task.get("error") and "[DRY_RUN]" in (task.get("error") or ""):
        result["passed"] = True
        result["notes"] = "Dry run — no live validation"
        return result

    if not url:
        result["notes"] = "No URL to validate"
        return result

    try:
        async with httpx.AsyncClient(
            timeout=15,
            follow_redirects=True,
            headers={"User-Agent": settings.crawler_user_agent},
        ) as client:
            resp = await client.get(url)

        accessible = resp.status_code == 200
        result["checks"]["accessible"] = accessible
        if not accessible:
            result["notes"] = f"Page returned {resp.status_code}"
            return result

        html = resp.text
        soup = BeautifulSoup(html, "lxml")

        # Validate by change type
        if change_type == "meta_title":
            meta_tag = soup.find("meta", property="og:title") or soup.find("title")
            current = meta_tag.get("content", meta_tag.get_text(strip=True)) if meta_tag else ""
            # For Yoast: check title tag content
            title_tag = soup.find("title")
            current_title = title_tag.get_text(strip=True) if title_tag else ""
            check_passed = (
                expected_value[:30].lower() in current_title.lower()
                or expected_value[:30].lower() in current.lower()
            )
            result["checks"]["meta_title_present"] = check_passed
            result["passed"] = accessible and check_passed

        elif change_type == "meta_description":
            desc_tag = soup.find("meta", attrs={"name": "description"})
            current_desc = desc_tag.get("content", "") if desc_tag else ""
            check_passed = expected_value[:50].lower() in current_desc.lower()
            result["checks"]["meta_desc_present"] = check_passed
            result["passed"] = accessible and check_passed

        elif change_type == "schema":
            has_schema = bool(soup.find("script", type="application/ld+json"))
            result["checks"]["schema_present"] = has_schema
            result["passed"] = accessible and has_schema

        elif change_type in ("content_rewrite", "new_section"):
            # Check word count didn't plummet (basic sanity)
            text = soup.get_text()
            word_count = len(text.split())
            result["checks"]["word_count"] = word_count
            result["checks"]["content_present"] = word_count > 50
            result["passed"] = accessible and word_count > 50

        else:
            result["passed"] = accessible

    except Exception as exc:
        result["notes"] = f"Validation error: {exc}"
        result["passed"] = False

    return result


async def validation_node(state: SEOAgentState) -> dict:
    """LangGraph node: validates applied changes and triggers rollback on failure."""

    logger.info("validation_agent.start")
    errors: list[str] = list(state.get("errors", []))
    applied = state.get("applied_changes", [])

    if not applied:
        return {
            "validation_results": [],
            "rollback_triggered": [],
            "errors": errors,
        }

    # Run all validations concurrently
    validation_coros = [_validate_task(task) for task in applied]
    raw_results = await asyncio.gather(*validation_coros, return_exceptions=True)

    validation_results: list[dict[str, Any]] = []
    rollback_triggered: list[str] = []

    for i, vr in enumerate(raw_results):
        if isinstance(vr, Exception):
            errors.append(f"Validation exception: {vr}")
            continue

        validation_results.append(vr)
        task = applied[i]

        if not vr["passed"] and settings.auto_rollback:
            snapshot = task.get("snapshot")
            if snapshot and task.get("wp_post_id") and task.get("change_type") != "new_page":
                try:
                    rollback_result = await wp_restore_snapshot.ainvoke(
                        {
                            "wp_post_id": task["wp_post_id"],
                            "snapshot": snapshot,
                        }
                    )
                    if rollback_result.get("success"):
                        rollback_triggered.append(task["task_id"])
                        logger.info(
                            "validation_agent.rollback",
                            task_id=task["task_id"],
                            url=task["page_url"],
                        )
                    else:
                        errors.append(
                            f"Rollback failed for {task['task_id']}: {rollback_result.get('error')}"
                        )
                except Exception as exc:
                    errors.append(f"Rollback exception for {task['task_id']}: {exc}")

    passed_count = sum(1 for vr in validation_results if vr.get("passed"))
    failed_count = len(validation_results) - passed_count

    # ── LLM validation summary ─────────────────────────────────────────────
    summary_items = [
        f"- {vr['url']} ({vr['change_type']}): {'PASS' if vr['passed'] else 'FAIL'} {vr.get('notes', '')}"
        for vr in validation_results[:10]
    ]

    llm_response = await _llm.ainvoke(
        [
            _SYSTEM_PROMPT,
            HumanMessage(
                content=f"Validation Summary for {state['target_url']}:\n"
                f"Passed: {passed_count}, Failed: {failed_count}\n"
                f"Rollbacks triggered: {len(rollback_triggered)}\n\n"
                "Results:\n" + "\n".join(summary_items) + "\n\n"
                "Provide a brief assessment of the validation outcome and any recommendations."
            ),
        ]
    )
    ai_notes = llm_response.content if hasattr(llm_response, "content") else str(llm_response)

    logger.info(
        "validation_agent.complete",
        passed=passed_count,
        failed=failed_count,
        rollbacks=len(rollback_triggered),
    )

    print(f"################################\nValidation complete. {passed_count} passed, {failed_count} failed. {len(rollback_triggered)} rollback(s) triggered. AI notes: {ai_notes} ################################")

    return {
        "validation_results": validation_results,
        "rollback_triggered": rollback_triggered,
        "errors": errors,
        "messages": [
            HumanMessage(
                content=f"Validation complete. {passed_count} passed, {failed_count} failed. "
                f"{len(rollback_triggered)} rollback(s) triggered.\n\n{ai_notes}"
            )
        ],
    }
