"""Fix Executor Agent — applies approved SEO changes to WordPress."""

from __future__ import annotations

import json
from typing import Any

import structlog
from langchain_anthropic import ChatAnthropic  # noqa: F401  # production provider
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage

from src.agents.state import ExecutionTask, SEOAgentState
from src.agents.validation_agent import _validate_task
from src.config import get_settings
from src.tools.html_merger import merge_content_rewrite, merge_new_section, merge_schema
from src.wordpress.client import WordPressClient as _WPClient
from src.tools.wordpress_tools import (
    wp_create_post,
    wp_restore_snapshot,
    wp_snapshot_page,
    wp_update_content,
    wp_update_schema,
    wp_update_seo_meta,
)

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

_SYSTEM_PROMPT = SystemMessage(content="""You are the Fix Executor Agent for an SEO automation system.
Your role is to safely apply approved SEO changes to WordPress sites.

Safety rules:
1. ALWAYS capture a snapshot before applying any change (for rollback)
2. Log every action with before/after values
3. In dry_run mode: simulate and report what WOULD be done, never write
4. Skip changes that would create duplicate meta titles across the site
5. Report any errors immediately without proceeding with the batch
6. Never modify content in a way that changes the page's factual meaning""")


async def _fetch_current_html(wp_id: int, post_type: str) -> str:
    """Return the current rendered content HTML for a post or page."""
    client = _WPClient()
    try:
        data = await client.get_page(wp_id) if post_type == "page" else await client.get_post(wp_id)
        return data.get("content", {}).get("rendered", "") or ""
    except Exception as exc:
        logger.warning("fix_executor.fetch_html_failed", wp_post_id=wp_id, error=str(exc))
        return ""


async def _check_ai_meta_preflight(tasks: list[ExecutionTask]) -> str | None:
    """Verify the WordPress plugin registered ai_* REST fields before writes."""
    meta_change_types = {
        "meta_title",
        "meta_description",
        "focus_keyword",
        "canonical_url",
        "og_title",
        "og_description",
        "og_image",
        "schema",
    }
    task = next(
        (
            t
            for t in tasks
            if t.get("change_type") in meta_change_types and t.get("wp_post_id")
        ),
        None,
    )
    if not task:
        return None

    client = _WPClient()
    result = await client.check_ai_meta_support(
        int(task["wp_post_id"]),
        task.get("post_type", "post"),
    )
    if result.get("supported"):
        return None

    missing = ", ".join(result.get("missing_fields") or [])
    extra = f" REST check error: {result['error']}." if result.get("error") else ""
    return (
        "SEO Agent WordPress plugin is not installed/active, or its ai_* REST fields "
        f"are not exposed for this post type. Missing fields: {missing}.{extra} "
        "Install/activate wp-plugin/seo-agent-meta.php before applying SEO meta/schema changes."
    )


def _task_page_key(task: ExecutionTask) -> tuple[str, int, str]:
    """Stable page identifier used for intra-run dependency batching."""
    return (
        task.get("page_url", ""),
        int(task.get("wp_post_id", 0)),
        task.get("post_type", "post"),
    )


def _split_dependency_batches(sorted_tasks: list[ExecutionTask]) -> tuple[list[list[ExecutionTask]], bool]:
    """Split tasks into safe dependency batches.

    Rule:
    - Each high-risk content_rewrite is executed alone, with validation before
      the next batch. This prevents one rewrite defect from propagating to
      multiple pages before review/validation can intervene.
    - If a page has content_rewrite + any of meta_title/meta_description/focus_keyword
      in the same run, isolate its content_rewrite in an earlier batch.
    """
    meta_types = {"meta_title", "meta_description", "focus_keyword"}
    high_risk_content_batches: list[list[ExecutionTask]] = []
    remaining_tasks: list[ExecutionTask] = []
    page_has_content_rewrite: set[tuple[str, int, str]] = set()
    page_has_meta: set[tuple[str, int, str]] = set()

    for task in sorted_tasks:
        page_key = _task_page_key(task)
        if task["change_type"] == "content_rewrite":
            page_has_content_rewrite.add(page_key)
            if task.get("risk_level") == "high":
                high_risk_content_batches.append([task])
                continue
        if task["change_type"] in meta_types:
            page_has_meta.add(page_key)
        remaining_tasks.append(task)

    needs_isolation = page_has_content_rewrite.intersection(page_has_meta)
    if not needs_isolation:
        batches = high_risk_content_batches + ([remaining_tasks] if remaining_tasks else [])
        return batches, bool(high_risk_content_batches)

    first_batch: list[ExecutionTask] = []
    second_batch: list[ExecutionTask] = []

    for task in remaining_tasks:
        page_key = _task_page_key(task)
        if page_key in needs_isolation and task["change_type"] == "content_rewrite":
            first_batch.append(task)
        else:
            second_batch.append(task)

    batches = high_risk_content_batches + [batch for batch in (first_batch, second_batch) if batch]
    return batches, True


async def _run_live_risk_check(batch: list[ExecutionTask]) -> str | None:
    """Return halt reason when LLM flags a non-ordering blocking risk."""
    if not batch:
        return None

    task_summary = json.dumps(
        [
            {
                "task_id": t["task_id"],
                "change_type": t["change_type"],
                "risk_level": t["risk_level"],
                "url": t["page_url"],
            }
            for t in batch
        ],
        indent=2,
    )
    risk_check = await _llm.ainvoke(
        [
            _SYSTEM_PROMPT,
            HumanMessage(
                content=f"Review this batch of {len(batch)} SEO changes for cumulative risk.\n\n"
                f"Tasks:\n{task_summary}\n\n"
                "Are there any high-risk combinations or ordering issues? "
                "Respond with: SAFE_TO_PROCEED or HALT: <reason>"
            ),
        ]
    )
    risk_response = risk_check.content if hasattr(risk_check, "content") else str(risk_check)
    if "HALT" not in risk_response:
        return None

    halt_reason = risk_response.split("HALT:", 1)[-1].strip()
    ordering_only_signals = (
        "ordering",
        "order",
        "execute first",
        "must be executed first",
        "sequenced",
        "prior batch",
        "isolate",
        "verification gate",
        "rollback misalignment",
    )
    lower_reason = halt_reason.lower()
    if any(sig in lower_reason for sig in ordering_only_signals):
        logger.warning(
            "fix_executor.risk_check_ignored",
            reason=halt_reason,
            note="Ignoring ordering-only HALT; deterministic batching/order is enforced in code.",
        )
        return None

    return halt_reason


async def _verify_and_guard_isolated_content_batch(
    applied_batch: list[ExecutionTask],
    errors: list[str],
) -> tuple[list[ExecutionTask], list[ExecutionTask], bool]:
    """Validate isolated content_rewrite tasks before allowing dependent meta batch.

    Returns:
        (still_applied, failed_from_gate, should_halt)
    """
    content_tasks = [t for t in applied_batch if t.get("change_type") == "content_rewrite"]
    if not content_tasks:
        return applied_batch, [], False

    failed_gate: list[ExecutionTask] = []
    still_applied: list[ExecutionTask] = []

    for task in applied_batch:
        if task.get("change_type") != "content_rewrite":
            still_applied.append(task)
            continue

        validation = await _validate_task(task)
        if validation.get("passed"):
            still_applied.append(task)
            continue

        rollback_ok = False
        snapshot = task.get("snapshot")
        if snapshot and task.get("wp_post_id"):
            rollback_result = await wp_restore_snapshot.ainvoke(
                {
                    "wp_post_id": task["wp_post_id"],
                    "snapshot": snapshot,
                }
            )
            rollback_ok = bool(rollback_result.get("success"))

        failed_task = dict(task)  # type: ignore[assignment]
        note = validation.get("notes") or "post-apply verification failed"
        rollback_note = "rolled back" if rollback_ok else "rollback failed"
        failed_task["error"] = f"Verification gate failed: {note} ({rollback_note})"
        failed_gate.append(failed_task)  # type: ignore[arg-type]
        errors.append(f"Task {task['task_id']} failed verification gate: {failed_task['error']}")

    return still_applied, failed_gate, bool(failed_gate)

def _sanitize_meta(task: dict) -> dict:
    """Validate and sanitize SEO fields before applying."""

    value = task.get("new_value", "")
    change_type = task.get("change_type")
    
    if not value.strip():
        task["error"] = "Empty value not allowed"
        task["approved"] = False

    if not value:
        return task

    # ── Title validation ─────────────────────
    if change_type == "meta_title":
        value = value.strip()
        if len(value) > 65:
            value = value[:65].rstrip()

    # ── Description validation ───────────────
    elif change_type == "meta_description":
        value = value.strip()
        if len(value) > 160:
            value = value[:160].rstrip()

    # ── Canonical URL validation ─────────────
    elif change_type == "canonical_url":
        if not value.startswith("http"):
            task["error"] = "Invalid canonical URL"
            task["approved"] = False
            return task

    # ── OG Image validation ──────────────────
    elif change_type == "og_image":
        if not value.startswith("http"):
            task["error"] = "Invalid OG image URL"
            task["approved"] = False
            return task

    # ── Schema validation ────────────────────
    elif change_type == "schema":
        try:
            json.loads(value)
        except Exception:
            task["error"] = "Invalid schema JSON"
            task["approved"] = False
            return task

    task["new_value"] = value
    return task

async def _apply_task(
    task: ExecutionTask,
    mode: str,
    existing_snapshots: dict[str, dict],
) -> ExecutionTask:
    """Apply a single execution task. Returns updated task."""
    wp_id = task["wp_post_id"]
    post_type = task.get("post_type", "post")
    change_type = task["change_type"]
    task = dict(task)  # type: ignore[assignment]

    # ── Dry run mode ──────────────────────────────────────────────────────
    if mode == "dry_run":
        logger.info(
            "fix_executor.dry_run",
            task_id=task["task_id"],
            change_type=change_type,
            wp_post_id=wp_id,
            field=task["field_name"],
            new_value=task["new_value"][:100] if task.get("new_value") else "",
        )
        task["applied"] = True  # Marked as "applied" in dry run (simulated)
        task["error"] = "[DRY_RUN] Change simulated - not applied to WordPress"
        return task  # type: ignore[return-value]

    # ── Snapshot before change ────────────────────────────────────────────
    snapshot_key = f"{wp_id}:{post_type}"
    if snapshot_key not in existing_snapshots and change_type != "new_page":
        snap_result = await wp_snapshot_page.ainvoke(
            {"wp_post_id": wp_id, "post_type": post_type}
        )
        if snap_result.get("success"):
            existing_snapshots[snapshot_key] = snap_result["snapshot"]
            task["snapshot"] = snap_result["snapshot"]
        else:
            task["error"] = f"Snapshot failed: {snap_result.get('error')}"
            return task  # type: ignore[return-value]

    # ── Apply change ──────────────────────────────────────────────────────
    try:
        if change_type in (
            "meta_title",
            "meta_description",
            "focus_keyword",
            "canonical_url",
            "og_title",
            "og_description",
            "og_image",
        ):
            kwargs: dict[str, Any] = {
                "wp_post_id": wp_id,
                "post_type": post_type,
            }

            if change_type == "meta_title":
                kwargs["seo_title"] = task["new_value"]

            elif change_type == "meta_description":
                kwargs["meta_description"] = task["new_value"]

            elif change_type == "focus_keyword":
                kwargs["focus_keyword"] = task["new_value"]

            elif change_type == "canonical_url":
                kwargs["canonical_url"] = task["new_value"]

            elif change_type == "og_title":
                kwargs["og_title"] = task["new_value"]

            elif change_type == "og_description":
                kwargs["og_description"] = task["new_value"]

            elif change_type == "og_image":
                kwargs["og_image"] = task["new_value"]

            result = await wp_update_seo_meta.ainvoke(kwargs)

        elif change_type == "content_rewrite":
            # Fetch existing content and let the merger decide: targeted section
            # replacement if there's a strong semantic match, full-page replace otherwise.
            existing = await _fetch_current_html(wp_id, post_type)
            merged = merge_content_rewrite(existing, task["new_value"])
            result = await wp_update_content.ainvoke(
                {"wp_post_id": wp_id, "post_type": post_type, "new_content": merged}
            )

        elif change_type == "new_section":
            # Append new section to existing content — never overwrite the page
            existing = await _fetch_current_html(wp_id, post_type)
            merged = merge_new_section(existing, task["new_value"])
            result = await wp_update_content.ainvoke(
                {"wp_post_id": wp_id, "post_type": post_type, "new_content": merged}
            )

        elif change_type == "schema":
            # Store schema in ai_schema_json meta field — PHP plugin renders it
            # in <head> as <script type="application/ld+json">.
            # Never injected into body content.
            result = await wp_update_schema.ainvoke(
                {"wp_post_id": wp_id, "post_type": post_type, "schema_json": task["new_value"]}
            )

        elif change_type == "new_page":
            # Parse the new page data
            page_data = json.loads(task["new_value"])
            result = await wp_create_post.ainvoke(
                {
                    "title": page_data.get("title", "New Page"),
                    "content": page_data.get("content", ""),
                    "post_type": post_type,
                    "slug": page_data.get("slug", ""),
                    "seo_title": page_data.get("meta_title", ""),
                    "meta_description": page_data.get("meta_description", ""),
                    "focus_keyword": page_data.get("focus_keyword", ""),
                    "status": "draft",  # Always create as draft for review
                }
            )
        else:
            task["error"] = f"Unknown change_type: {change_type}"
            return task  # type: ignore[return-value]

        if result.get("success"):
            task["applied"] = True
            task["error"] = None
            logger.info(
                "fix_executor.applied",
                task_id=task["task_id"],
                change_type=change_type,
                wp_post_id=wp_id,
            )
        else:
            task["error"] = result.get("error", "Unknown error")
            logger.error(
                "fix_executor.apply_failed",
                task_id=task["task_id"],
                error=task["error"],
            )

    except Exception as exc:
        task["error"] = str(exc)
        logger.error("fix_executor.exception", task_id=task["task_id"], error=str(exc))

    return task  # type: ignore[return-value]


async def fix_executor_node(state: SEOAgentState) -> dict:
    """LangGraph node: applies approved execution tasks to WordPress."""

    mode = state.get("mode", settings.execution_mode)
    approval_status = state.get("approval_status", "pending")
    tasks = state.get("execution_tasks", [])

    logger.info(
        "fix_executor_agent.start",
        mode=mode,
        approval_status=approval_status,
        total_tasks=len(tasks),
    )

    errors: list[str] = list(state.get("errors", []))

    if approval_status != "approved":
        logger.info("fix_executor_agent.skipped", reason="not_approved")
        return {
            "applied_changes": [],
            "failed_changes": [],
            "errors": errors,
            "messages": [
                HumanMessage(
                    content=f"Fix Executor skipped — approval status: {approval_status}. "
                    "Approve the execution plan to apply changes."
                )
            ],
        }

    if not tasks:
        return {
            "applied_changes": [],
            "failed_changes": [],
            "errors": errors,
        }

    preflight_failed: list[ExecutionTask] = []
    if mode in ("preview", "live"):
        plugin_error = await _check_ai_meta_preflight(tasks)
        if plugin_error:
            errors.append(plugin_error)
            preflight_failed = [
                {**task, "error": plugin_error}
                for task in tasks
                if task.get("change_type")
                in {
                    "meta_title",
                    "meta_description",
                    "focus_keyword",
                    "canonical_url",
                    "og_title",
                    "og_description",
                    "og_image",
                    "schema",
                }
            ]
            content_tasks = [
                task
                for task in tasks
                if task.get("change_type")
                not in {
                    "meta_title",
                    "meta_description",
                    "focus_keyword",
                    "canonical_url",
                    "og_title",
                    "og_description",
                    "og_image",
                    "schema",
                }
            ]
            if not content_tasks:
                return {
                    "applied_changes": [],
                    "failed_changes": preflight_failed,
                    "errors": errors,
                }
            tasks = content_tasks

    # Pre-compute deterministic ordering so both the AI risk check and executor
    # evaluate the same sequence.
    type_order = {
        "content_rewrite": 0,
        "new_section": 0,
        "schema": 1,
        "meta_title": 2,
        "meta_description": 2,
        "focus_keyword": 2,
        "new_page": 3,
    }
    risk_order = {"low": 0, "medium": 1, "high": 2}
    sorted_tasks = sorted(
        tasks,
        key=lambda t: (
            type_order.get(t["change_type"], 1),
            risk_order.get(t["risk_level"], 1),
        ),
    )

    dependency_batches, has_isolated_content_phase = _split_dependency_batches(sorted_tasks)
    logger.info(
        "fix_executor_agent.batches",
        total_batches=len(dependency_batches),
        isolated_content_phase=has_isolated_content_phase,
    )

    # ── Execute tasks (possibly in dependency batches) ──────────────────────
    applied: list[ExecutionTask] = []
    failed: list[ExecutionTask] = list(preflight_failed)
    snapshots: dict[str, dict] = {}

    for idx, batch in enumerate(dependency_batches):
        batch_number = idx + 1
        logger.info("fix_executor_agent.batch_start", batch_number=batch_number, tasks=len(batch))

        if mode in ("preview", "live"):
            halt_reason = await _run_live_risk_check(batch)
            if halt_reason:
                errors.append(f"Fix Executor halted by AI risk check: {halt_reason}")
                failed.extend(batch)
                break

        batch_applied: list[ExecutionTask] = []

        for task in batch:
            if not task.get("approved", False):
                continue

            # Apply validation
            task = _sanitize_meta(task)

            # Skip if validation failed
            if task.get("error"):
                failed.append(task)
                errors.append(f"Task {task['task_id']} failed validation: {task['error']}")
                continue

            updated = await _apply_task(task, mode, snapshots)
            update_error = updated.get("error") or ""
            simulated = "[DRY_RUN]" in update_error
            if updated.get("applied") and (not update_error or simulated):
                applied.append(updated)  # type: ignore[arg-type]
                batch_applied.append(updated)  # type: ignore[arg-type]
            else:
                if mode != "dry_run" or not updated.get("applied"):
                    failed.append(updated)  # type: ignore[arg-type]
                    errors.append(f"Task {updated['task_id']} failed: {updated.get('error')}")
                else:
                    applied.append(updated)  # type: ignore[arg-type]
                    batch_applied.append(updated)  # type: ignore[arg-type]

        # Gate: content rewrite must verify before dependent meta tasks are allowed.
        if (
            mode in ("preview", "live")
            and has_isolated_content_phase
            and any(t.get("change_type") == "content_rewrite" for t in batch)
        ):
            _, gate_failed, should_halt = await _verify_and_guard_isolated_content_batch(
                batch_applied,
                errors,
            )
            if gate_failed:
                failed.extend(gate_failed)
                failed_ids = {t["task_id"] for t in gate_failed}
                applied = [t for t in applied if t["task_id"] not in failed_ids]
            if should_halt:
                remaining = [t for later in dependency_batches[idx + 1 :] for t in later]
                failed.extend(remaining)
                errors.append(
                    "Fix Executor halted by dependency gate: isolated content_rewrite verification failed; "
                    "dependent batch not executed."
                )
                break

    logger.info(
        "fix_executor_agent.complete",
        applied=len(applied),
        failed=len(failed),
        mode=mode,
    )

    print(f"################################\nFix Executor complete [{mode} mode]. {len(applied)} changes applied, {len(failed)} failed. Major errors encountered: {errors}. Applied changes: {applied} and the failed changes: {failed}################################")

    return {
        "applied_changes": applied,
        "failed_changes": failed,
        "errors": errors,
        "messages": [
            HumanMessage(
                content=f"Fix Executor complete [{mode} mode]. "
                f"{len(applied)} changes applied, {len(failed)} failed."
            )
        ],
    }
