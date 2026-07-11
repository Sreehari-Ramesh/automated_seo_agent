"""Run the full SEO graph for the whole website (not a single page).

Despite the filename, this script now performs a site-wide run starting from
TARGET_SITE_URL and lets the crawler discover all pages.
"""

import asyncio
import json
import uuid

from src.database.db import init_db
from src.graph.seo_graph import build_initial_state, get_compiled_graph

TARGET_SITE_URL = "https://staging.gaminggodfather.com/"

# False -> dry_run (no writes)
# True  -> live (apply changes; use carefully)
LIVE_MODE = False


async def main():
    await init_db()

    run_id = str(uuid.uuid4())
    mode = "live" if LIVE_MODE else "dry_run"
    approval_status = "approved" if LIVE_MODE else "pending"

    print(f"Target: {TARGET_SITE_URL}")
    print(f"Mode: {mode.upper()} | Approval: {approval_status}")

    initial = build_initial_state(
        run_id=run_id,
        thread_id=run_id,
        site_id=1,
        target_url=TARGET_SITE_URL,
        mode=mode,
        approval_status=approval_status,
    )

    # skip_human_interrupt=True keeps this script one-shot for local testing.
    graph = await get_compiled_graph(skip_human_interrupt=True)
    config = {"configurable": {"thread_id": run_id}}
    result = await graph.ainvoke(initial, config=config)

    print(f"\nSEO Score:   {result.get('overall_seo_score', 0)}/100")
    print(f"Pages:       {len(result.get('crawled_pages', []))}")
    print(f"Issues:      {len(result.get('issues', []))}")
    print(f"Tasks:       {len(result.get('execution_tasks', []))}")

    if LIVE_MODE:
        applied = result.get("applied_changes", [])
        failed = result.get("failed_changes", [])
        halt_errors = [
            e
            for e in result.get("errors", [])
            if "halted by" in e.lower() or "halt" in e.lower()
        ]
        if halt_errors:
            print(f"\nLIVE - HALTED by safety check (0 applied)")
            for e in halt_errors:
                print(f"  REASON: {e[:200]}")
        else:
            print(f"\nLIVE - Applied: {len(applied)} | Failed: {len(failed)}")
    else:
        print("\nDRY RUN - No changes applied.")

    with open("single_page_result.json", "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, default=str)
    print("\nFull results saved to single_page_result.json")


if __name__ == "__main__":
    asyncio.run(main())
