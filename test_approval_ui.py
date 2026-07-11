"""Local test helper — seeds a fake approval request and prints the review URL.

Run this BEFORE opening the approval UI in a browser so there is something to review.

Usage:
    python test_approval_ui.py

Then open the URL it prints in your browser while the FastAPI server is running:
    uvicorn src.api.main:app --reload --port 8000
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta

from src.config import get_settings
from src.database.db import init_db, AsyncSessionLocal
from src.models.workflow import ApprovalRequest, WorkflowRun

settings = get_settings()

# ── Fake task data ────────────────────────────────────────────────────────────
_FAKE_TASKS = [
    {
        "page_url": "https://example.com/about/",
        "change_type": "meta_title",
        "old_value": "About Us",
        "new_value": "About Us | Expert WordPress SEO Services",
        "risk_level": "low",
    },
    {
        "page_url": "https://example.com/about/",
        "change_type": "meta_description",
        "old_value": "",
        "new_value": (
            "Learn about our team of SEO specialists. We help WordPress sites "
            "rank higher, drive organic traffic, and convert visitors into customers."
        ),
        "risk_level": "low",
    },
    {
        "page_url": "https://example.com/services/",
        "change_type": "new_section",
        "old_value": "",
        "new_value": (
            "<section>\n"
            "  <h2>Why Choose Our SEO Services?</h2>\n"
            "  <p>Our data-driven approach combines technical audits, content "
            "optimisation, and competitor analysis to deliver measurable results. "
            "We've helped over 200 WordPress sites achieve page-one rankings.</p>\n"
            "</section>"
        ),
        "risk_level": "medium",
    },
    {
        "page_url": "https://example.com/blog/seo-guide/",
        "change_type": "content_rewrite",
        "old_value": "<p>SEO is important for websites.</p>",
        "new_value": (
            "<p>Search engine optimisation (SEO) is the process of improving a "
            "website's visibility in organic search results. For WordPress sites, "
            "this means optimising meta tags, site speed, internal linking, and "
            "content depth — all areas where small improvements compound over time.</p>"
        ),
        "risk_level": "high",
    },
    {
        "page_url": "https://example.com/",
        "change_type": "schema",
        "old_value": "",
        "new_value": (
            '{\n  "@context": "https://schema.org",\n'
            '  "@type": "Organization",\n'
            '  "name": "Example Co",\n'
            '  "url": "https://example.com"\n}'
        ),
        "risk_level": "low",
    },
]


async def _seed() -> None:
    await init_db()

    run_id = f"test-{uuid.uuid4().hex[:8]}"
    thread_id = f"thread-{uuid.uuid4().hex[:8]}"

    async with AsyncSessionLocal() as db:
        # Create a fake run in waiting_for_approval state
        run = WorkflowRun(
            run_id=run_id,
            thread_id=thread_id,
            site_id=1,
            target_url="https://example.com",
            mode="dry_run",
            status="waiting_for_approval",
            current_node="human_review",
            approval_status="pending",
        )
        db.add(run)
        await db.flush()

        # Create the approval request
        plan_summary = {
            "total_tasks": len(_FAKE_TASKS),
            "tasks": _FAKE_TASKS,
            "risk_breakdown": {
                "low": sum(1 for t in _FAKE_TASKS if t["risk_level"] == "low"),
                "medium": sum(1 for t in _FAKE_TASKS if t["risk_level"] == "medium"),
                "high": sum(1 for t in _FAKE_TASKS if t["risk_level"] == "high"),
            },
        }
        approval = ApprovalRequest(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            run_id=run_id,
            plan_summary=plan_summary,
            tasks_count=len(_FAKE_TASKS),
            status="pending",
            expires_at=datetime.utcnow() + timedelta(hours=72),
        )
        db.add(approval)
        await db.commit()

    token_param = f"?token={settings.api_secret}" if settings.api_secret else ""
    url = f"http://localhost:{settings.api_port}/review/{run_id}{token_param}"

    print()
    print("=" * 60)
    print("Fake approval seeded successfully.")
    print(f"  run_id : {run_id}")
    print(f"  tasks  : {len(_FAKE_TASKS)}")
    print()
    print("Open this URL in your browser:")
    print(f"  {url}")
    print()
    print("Make sure the FastAPI server is running:")
    print(f"  uvicorn src.api.main:app --reload --port {settings.api_port}")
    print("=" * 60)
    print()


if __name__ == "__main__":
    asyncio.run(_seed())
