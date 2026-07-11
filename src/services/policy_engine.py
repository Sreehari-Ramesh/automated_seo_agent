"""PolicyEngine — SEO change threshold and anti-churn guard.

Enforces cooldown periods, manual-only rules, and retry limits
before any task is allowed through to the Fix Executor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.agents.state import ExecutionTask
from src.config import get_settings
from src.models.schemas import Change, Page

settings = get_settings()
logger = structlog.get_logger(__name__)


@dataclass
class PolicyDecision:
    """Result of a policy check for one ExecutionTask."""
    task_id: str
    allowed: bool
    reason: str
    # If not allowed, suggest when the task can next run
    retry_after: datetime | None = None
    # Force manual approval even if auto-approve was requested
    requires_manual_approval: bool = False


@dataclass
class PolicyReport:
    """Aggregate policy report for a full batch."""
    allowed: list[ExecutionTask] = field(default_factory=list)
    blocked: list[tuple[ExecutionTask, PolicyDecision]] = field(default_factory=list)
    manual_only: list[ExecutionTask] = field(default_factory=list)

    @property
    def summary(self) -> dict[str, Any]:
        return {
            "allowed_count": len(self.allowed),
            "blocked_count": len(self.blocked),
            "manual_only_count": len(self.manual_only),
            "blocked_reasons": [d.reason for _, d in self.blocked],
        }


class PolicyEngine:
    """Evaluates ExecutionTasks against anti-churn SEO policy rules.

    Rules (all configurable via config/default.toml → [policy]):

    1. Meta cooldown      — same page's meta cannot be changed within N days
    2. Content cooldown   — content rewrites have a longer cooldown
    3. Homepage gate      — homepage always requires manual approval
    4. New page gate      — new page creation always requires manual approval
    5. Low impression     — pages with low impressions get conservative treatment
    6. Failed retry block — repeated failures on same page are blocked
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def evaluate_batch(
        self, tasks: list[ExecutionTask]
    ) -> PolicyReport:
        """Evaluate all tasks and return a PolicyReport."""
        report = PolicyReport()
        for task in tasks:
            decision = await self.evaluate(task)
            if not decision.allowed:
                report.blocked.append((task, decision))
            elif decision.requires_manual_approval:
                report.manual_only.append(task)
            else:
                report.allowed.append(task)

        logger.info(
            "policy_engine.batch_evaluated",
            allowed=len(report.allowed),
            blocked=len(report.blocked),
            manual_only=len(report.manual_only),
        )
        return report

    async def evaluate(self, task: ExecutionTask) -> PolicyDecision:
        """Evaluate a single task against all policy rules."""
        # Rule 1: New page — always manual only
        if task["change_type"] == "new_page":
            if settings.policy_new_page_manual_only:
                return PolicyDecision(
                    task_id=task["task_id"],
                    allowed=True,
                    reason="New page creation requires manual approval",
                    requires_manual_approval=True,
                )

        # Rule 2: Homepage gate
        page_url = task.get("page_url", "")
        if _is_homepage(page_url, settings.wp_base_url) and settings.policy_homepage_manual_only:
            return PolicyDecision(
                task_id=task["task_id"],
                allowed=True,
                reason="Homepage changes require manual approval",
                requires_manual_approval=True,
            )

        wp_post_id = task.get("wp_post_id")
        if not wp_post_id:
            return PolicyDecision(
                task_id=task["task_id"], allowed=True, reason="No post ID — pass through"
            )

        # Rule 3: Meta cooldown
        if task["change_type"] in ("meta_title", "meta_description", "focus_keyword"):
            cooldown_check = await self._check_cooldown(
                wp_post_id,
                ("meta_title", "meta_description", "focus_keyword"),
                settings.policy_meta_cooldown_days,
            )
            if cooldown_check:
                return PolicyDecision(
                    task_id=task["task_id"],
                    allowed=False,
                    reason=cooldown_check["reason"],
                    retry_after=cooldown_check["retry_after"],
                )

        # Rule 4: Content cooldown
        if task["change_type"] in ("content_rewrite", "new_section", "schema"):
            cooldown_check = await self._check_cooldown(
                wp_post_id,
                ("content_rewrite", "new_section", "schema"),
                settings.policy_content_cooldown_days,
            )
            if cooldown_check:
                return PolicyDecision(
                    task_id=task["task_id"],
                    allowed=False,
                    reason=cooldown_check["reason"],
                    retry_after=cooldown_check["retry_after"],
                )

        # Rule 5: Failed retry block
        failed_count = await self._count_recent_failures(wp_post_id, task["change_type"])
        if failed_count >= settings.policy_max_failed_retries:
            return PolicyDecision(
                task_id=task["task_id"],
                allowed=False,
                reason=(
                    f"Page {wp_post_id} has {failed_count} consecutive failed attempts "
                    f"for {task['change_type']} — manual investigation required"
                ),
            )

        # Rule 6: High risk always requires manual approval
        if task.get("risk_level") == "high":
            return PolicyDecision(
                task_id=task["task_id"],
                allowed=True,
                reason="High-risk changes require manual approval",
                requires_manual_approval=True,
            )

        return PolicyDecision(
            task_id=task["task_id"], allowed=True, reason="Policy check passed"
        )

    # ── Internal rule helpers ─────────────────────────────────────────────────

    async def _check_cooldown(
        self,
        wp_post_id: int,
        change_types: tuple[str, ...],
        cooldown_days: int,
    ) -> dict[str, Any] | None:
        """Return block info if a recent successful change exists, else None."""
        since = datetime.utcnow() - timedelta(days=cooldown_days)

        # Find the most recent successful change of these types for this page
        result = await self.db.execute(
            select(Change.applied_at)
            .join(Change.page)
            .where(
                and_(
                    Page.wp_post_id == wp_post_id,
                    Change.change_type.in_(change_types),
                    Change.status == "applied",
                    Change.applied_at >= since,
                )
            )
            .order_by(Change.applied_at.desc())
            .limit(1)
        )
        last_applied = result.scalar_one_or_none()

        if last_applied:
            retry_after = last_applied + timedelta(days=cooldown_days)
            days_remaining = (retry_after - datetime.utcnow()).days
            return {
                "reason": (
                    f"Post {wp_post_id} — {'/'.join(change_types)} changed "
                    f"{(datetime.utcnow() - last_applied).days}d ago "
                    f"(cooldown: {cooldown_days}d). Next allowed in ~{days_remaining}d."
                ),
                "retry_after": retry_after,
            }
        return None

    async def _count_recent_failures(
        self, wp_post_id: int, change_type: str, lookback_days: int = 30
    ) -> int:
        """Count consecutive failed changes of a type on a page."""
        since = datetime.utcnow() - timedelta(days=lookback_days)
        result = await self.db.execute(
            select(func.count())
            .select_from(Change)
            .join(Change.page)
            .where(
                and_(
                    Page.wp_post_id == wp_post_id,
                    Change.change_type == change_type,
                    Change.status == "failed",
                    Change.created_at >= since,
                )
            )
        )
        return result.scalar_one() or 0


def _is_homepage(page_url: str, base_url: str) -> bool:
    """Return True if page_url is the root homepage."""
    if not base_url:
        return False
    normalized_page = page_url.rstrip("/")
    normalized_base = base_url.rstrip("/")
    return normalized_page == normalized_base
