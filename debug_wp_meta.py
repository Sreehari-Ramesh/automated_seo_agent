# debug_wp_meta.py — verify all Yoast meta fields persist for pages
# Run: python debug_wp_meta.py

import asyncio
import json
import httpx
import base64
from src.wordpress.client import WordPressClient
from src.config import get_settings

PAGE_ID = 347


async def main() -> None:
    wp = WordPressClient()
    settings = get_settings()
    creds = base64.b64encode(
        f"{settings.wp_username}:{settings.wp_app_password}".encode()
    ).decode()
    headers = {"Authorization": f"Basic {creds}", "Content-Type": "application/json"}

    # ── 1. Read current state ──────────────────────────────────────────────────
    print("── Step 1: Read current meta ─────────────────────────────────")
    data = await wp.get_page(PAGE_ID)
    yoast = data.get("yoast_head_json", {})
    meta  = data.get("meta", {})
    print(f"  yoast title  : {yoast.get('title')}")
    print(f"  yoast desc   : {yoast.get('description')}")
    print(f"  meta keys    : {list(meta.keys())}")

    # ── 2. Write all three fields ──────────────────────────────────────────────
    test = {
        "_yoast_wpseo_title":    "DEBUG_TITLE_9999",
        "_yoast_wpseo_metadesc": "DEBUG_DESC_9999",
        "_yoast_wpseo_focuskw":  "debug keyword",
    }
    print("\n── Step 2: PATCH meta via REST API ──────────────────────────────")
    async with httpx.AsyncClient() as client:
        r = await client.patch(
            f"{settings.wp_base_url}/wp-json/wp/v2/pages/{PAGE_ID}",
            headers=headers,
            json={"meta": test},
            timeout=15,
        )
    print(f"  status: {r.status_code}")
    resp = r.json()
    returned_meta = resp.get("meta", {})
    print(f"  returned meta keys : {list(returned_meta.keys())}")
    for k in test:
        print(f"  {k} = {repr(returned_meta.get(k, 'NOT RETURNED'))}")

    # ── 3. Read back ──────────────────────────────────────────────────────────
    print("\n── Step 3: Read back to verify ──────────────────────────────────")
    data2 = await wp.get_page(PAGE_ID)
    yoast2 = data2.get("yoast_head_json", {})
    meta2  = data2.get("meta", {})
    print(f"  yoast title  : {yoast2.get('title')}")
    print(f"  yoast desc   : {yoast2.get('description')}")
    for k in test:
        v = meta2.get(k, "NOT IN META")
        status = "✓" if v == test[k] else "✗"
        print(f"  {status} {k} = {repr(v)}")

    # ── Summary ────────────────────────────────────────────────────────────────
    print()
    saved = [k for k in test if meta2.get(k) == test[k]]
    failed = [k for k in test if meta2.get(k) != test[k]]
    if failed:
        print(f"FAILED keys: {failed}")
        print()
        print("FIX — update your mu-plugin/functions.php to register for 'post' AND 'page':")
        print("""
add_action('init', function() {
    $keys = ['_yoast_wpseo_title','_yoast_wpseo_metadesc','_yoast_wpseo_focuskw'];
    foreach (['post', 'page'] as $post_type) {
        foreach ($keys as $key) {
            register_meta($post_type, $key, [
                'show_in_rest'  => true,
                'single'        => true,
                'type'          => 'string',
                'auth_callback' => function() { return current_user_can('edit_posts'); },
            ]);
        }
    }
});
""")
    else:
        print("✓ ALL fields saved correctly")


asyncio.run(main())
