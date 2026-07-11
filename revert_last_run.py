# revert_last_run.py
# Restores the page to the state captured before the last live run.
# Run with: python revert_last_run.py

import asyncio
import json
from src.tools.wordpress_tools import wp_restore_snapshot
from src.database.db import init_db

RESULT_FILE = "single_page_result.json"


async def main() -> None:
    await init_db()

    with open(RESULT_FILE) as f:
        result = json.load(f)

    tasks = result.get("execution_tasks", [])
    seen: set[int] = set()

    for task in tasks:
        snap = task.get("snapshot")
        wp_id = task.get("wp_post_id")
        if not snap or not wp_id or wp_id in seen:
            continue
        seen.add(wp_id)
        print(f"Reverting page {wp_id} ({task.get('page_url', '')}) …")
        res = await wp_restore_snapshot.ainvoke({"wp_post_id": wp_id, "snapshot": snap})
        if res.get("success"):
            print(f"  OK — restored to snapshot taken at {snap.get('captured_at', 'unknown time')}")
        else:
            print(f"  FAILED — {res.get('error')}")

    if not seen:
        print("No snapshots found in result file — nothing to revert.")


asyncio.run(main())
