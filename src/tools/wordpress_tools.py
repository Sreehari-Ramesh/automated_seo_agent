"""LangChain tools that wrap the WordPress REST API client.

Uses custom ai_* meta fields (registered by seo-agent-meta.php) instead of
Yoast-private keys. The PHP plugin handles rendering in <head>.
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import tool

from src.wordpress.client import WordPressClient

# ── Custom SEO meta field names (must match seo-agent-meta.php) ───────────────
AI_META_TITLE       = "ai_meta_title"
AI_META_DESCRIPTION = "ai_meta_description"
AI_CANONICAL_URL    = "ai_canonical_url"
AI_OG_TITLE         = "ai_og_title"
AI_OG_DESCRIPTION   = "ai_og_description"
AI_OG_IMAGE         = "ai_og_image"
AI_SCHEMA_JSON      = "ai_schema_json"
AI_FOCUS_KEYWORD    = "ai_focus_keyword"


def _client() -> WordPressClient:
    return WordPressClient()


def _field_mismatches(response: dict[str, Any], expected: dict[str, str]) -> dict[str, str]:
    """Return fields missing from the REST response meta block."""
    actual = response.get("meta") or {}
    return {
        key: value
        for key, value in expected.items()
        if actual.get(key) != value
    }


def _content_update_visible(response: dict[str, Any], new_content: str, new_title: str) -> bool:
    if new_title:
        title = response.get("title") or {}
        if isinstance(title, dict) and title.get("raw") not in (None, new_title):
            return False
    if not new_content:
        return True
    content = response.get("content") or {}
    if not isinstance(content, dict):
        return False
    raw = content.get("raw")
    rendered = content.get("rendered", "")
    if raw == new_content or new_content in rendered:
        return True
    snippet = " ".join(new_content.split())[:120]
    rendered_normalized = " ".join(str(rendered).split())
    return bool(snippet and snippet in rendered_normalized)


@tool
async def wp_get_all_content(post_type: str = "both") -> dict[str, Any]:
    """Fetch all published posts and/or pages from WordPress.

    Args:
        post_type: 'posts', 'pages', or 'both'.

    Returns:
        Dict with posts (list), pages (list), total_count, error.
    """
    client = _client()
    try:
        posts: list[dict] = []
        pages: list[dict] = []
        if post_type in ("posts", "both"):
            p = 1
            while True:
                batch = await client.list_posts(per_page=100, page=p)
                if not batch:
                    break
                posts.extend(batch)
                if len(batch) < 100:
                    break
                p += 1
        if post_type in ("pages", "both"):
            p = 1
            while True:
                batch = await client.list_pages(per_page=100, page=p)
                if not batch:
                    break
                pages.extend(batch)
                if len(batch) < 100:
                    break
                p += 1
        return {
            "posts": posts,
            "pages": pages,
            "total_count": len(posts) + len(pages),
            "error": None,
        }
    except Exception as exc:
        return {"posts": [], "pages": [], "total_count": 0, "error": str(exc)}


@tool
async def wp_update_seo_meta(
    wp_post_id: int,
    post_type: str,
    seo_title: str = "",
    meta_description: str = "",
    focus_keyword: str = "",
    canonical_url: str = "",
    og_title: str = "",
    og_description: str = "",
    og_image: str = "",
) -> dict[str, Any]:
    """Update custom AI SEO meta fields for a WordPress post or page.

    Writes to ai_* meta fields registered by the seo-agent-meta.php plugin,
    which injects them into <head> and optionally suppresses Yoast output.

    Args:
        wp_post_id:       The WordPress post/page ID.
        post_type:        'post' or 'page'.
        seo_title:        SEO <title> tag value.
        meta_description: Meta description content.
        focus_keyword:    Primary focus keyword.
        canonical_url:    Canonical URL override.
        og_title:         Open Graph title (defaults to seo_title if empty).
        og_description:   Open Graph description.
        og_image:         Open Graph image URL.

    Returns:
        Dict with success, wp_post_id, updated_fields, error.
    """
    client = _client()
    meta: dict[str, str] = {}
    if seo_title:
        meta[AI_META_TITLE] = seo_title
    if meta_description:
        meta[AI_META_DESCRIPTION] = meta_description
    if focus_keyword:
        meta[AI_FOCUS_KEYWORD] = focus_keyword
    if canonical_url:
        meta[AI_CANONICAL_URL] = canonical_url
    if og_title:
        meta[AI_OG_TITLE] = og_title
    if og_description:
        meta[AI_OG_DESCRIPTION] = og_description
    if og_image:
        meta[AI_OG_IMAGE] = og_image

    if not meta:
        return {"success": False, "error": "No meta fields provided", "wp_post_id": wp_post_id}

    try:
        if post_type == "page":
            response = await client.update_page_meta(wp_post_id, meta)
        else:
            response = await client.update_post_meta(wp_post_id, meta)
        mismatches = _field_mismatches(response, meta)
        if mismatches:
            return {
                "success": False,
                "wp_post_id": wp_post_id,
                "updated_fields": [],
                "error": (
                    "WordPress accepted the request but did not persist these REST meta "
                    f"fields: {', '.join(mismatches)}. Confirm seo-agent-meta.php is "
                    "installed/active and the API user can edit this post type."
                ),
            }
        return {
            "success": True,
            "wp_post_id": wp_post_id,
            "updated_fields": list(meta.keys()),
            "error": None,
        }
    except Exception as exc:
        return {"success": False, "wp_post_id": wp_post_id, "error": str(exc)}


@tool
async def wp_update_schema(
    wp_post_id: int,
    post_type: str,
    schema_json: str,
) -> dict[str, Any]:
    """Write a JSON-LD schema object to the ai_schema_json meta field.

    The PHP plugin validates the JSON and renders it as a
    <script type="application/ld+json"> in <head> — keeps schema out of
    the page body content entirely.

    Args:
        wp_post_id:  The WordPress post/page ID.
        post_type:   'post' or 'page'.
        schema_json: Raw JSON string (Schema.org object).

    Returns:
        Dict with success, wp_post_id, error.
    """
    import json as _json
    try:
        _json.loads(schema_json)
    except ValueError as exc:
        return {"success": False, "wp_post_id": wp_post_id, "error": f"Invalid JSON: {exc}"}

    client = _client()
    try:
        if post_type == "page":
            response = await client.update_page_meta(wp_post_id, {AI_SCHEMA_JSON: schema_json})
        else:
            response = await client.update_post_meta(wp_post_id, {AI_SCHEMA_JSON: schema_json})
        mismatches = _field_mismatches(response, {AI_SCHEMA_JSON: schema_json})
        if mismatches:
            return {
                "success": False,
                "wp_post_id": wp_post_id,
                "error": (
                    "WordPress accepted the request but did not persist ai_schema_json. "
                    "Confirm seo-agent-meta.php is installed/active."
                ),
            }
        return {"success": True, "wp_post_id": wp_post_id, "error": None}
    except Exception as exc:
        return {"success": False, "wp_post_id": wp_post_id, "error": str(exc)}


@tool
async def wp_update_content(
    wp_post_id: int,
    post_type: str,
    new_content: str = "",
    new_title: str = "",
) -> dict[str, Any]:
    """Update the body content and/or title of a WordPress post or page.

    Args:
        wp_post_id:  The WordPress post/page ID.
        post_type:   'post' or 'page'.
        new_content: New HTML content body (empty = don't change).
        new_title:   New post title (empty = don't change).

    Returns:
        Dict with success, wp_post_id, error.
    """
    if not new_content and not new_title:
        return {"success": False, "error": "Nothing to update", "wp_post_id": wp_post_id}

    client = _client()
    try:
        if post_type == "page":
            response = await client.update_page_content(wp_post_id, new_content, new_title or None)
        else:
            response = await client.update_post_content(wp_post_id, new_content, new_title or None)
        if not _content_update_visible(response, new_content, new_title):
            return {
                "success": False,
                "wp_post_id": wp_post_id,
                "error": (
                    "WordPress accepted the content update but the REST response did not "
                    "show the expected content/title. Check editor/theme permissions, "
                    "page-builder storage, and REST API support for this post type."
                ),
            }
        return {"success": True, "wp_post_id": wp_post_id, "error": None}
    except Exception as exc:
        return {"success": False, "wp_post_id": wp_post_id, "error": str(exc)}


@tool
async def wp_create_post(
    title: str,
    content: str,
    post_type: str = "post",
    slug: str = "",
    seo_title: str = "",
    meta_description: str = "",
    focus_keyword: str = "",
    status: str = "draft",
) -> dict[str, Any]:
    """Create a new WordPress post or page with AI SEO meta fields.

    Args:
        title:            Post title.
        content:          HTML content body.
        post_type:        'post' or 'page'.
        slug:             URL slug (auto-generated if empty).
        seo_title:        AI SEO title.
        meta_description: AI meta description.
        focus_keyword:    AI focus keyword.
        status:           'draft' (default) or 'publish'.

    Returns:
        Dict with success, wp_post_id, url, error.
    """
    client = _client()
    meta: dict[str, str] = {}
    if seo_title:
        meta[AI_META_TITLE] = seo_title
    if meta_description:
        meta[AI_META_DESCRIPTION] = meta_description
    if focus_keyword:
        meta[AI_FOCUS_KEYWORD] = focus_keyword

    try:
        if post_type == "page":
            result = await client.create_page(
                title=title,
                content=content,
                slug=slug or None,
                meta=meta or None,
                status=status,
            )
        else:
            result = await client.create_post(
                title=title,
                content=content,
                slug=slug or None,
                meta=meta or None,
                status=status,
            )
        return {
            "success": True,
            "wp_post_id": result.get("id"),
            "url": result.get("link", ""),
            "error": None,
        }
    except Exception as exc:
        return {"success": False, "wp_post_id": None, "url": "", "error": str(exc)}


@tool
async def wp_snapshot_page(wp_post_id: int, post_type: str = "post") -> dict[str, Any]:
    """Capture the current SEO state of a post/page for rollback purposes."""
    client = _client()
    try:
        snapshot = await client.snapshot_post(wp_post_id, post_type)
        return {"success": True, "snapshot": snapshot, "error": None}
    except Exception as exc:
        return {"success": False, "snapshot": {}, "error": str(exc)}


@tool
async def wp_restore_snapshot(
    wp_post_id: int, snapshot: dict[str, Any]
) -> dict[str, Any]:
    """Restore a post/page to a previously captured snapshot."""
    client = _client()
    try:
        await client.restore_snapshot(wp_post_id, snapshot)
        return {"success": True, "wp_post_id": wp_post_id, "error": None}
    except Exception as exc:
        return {"success": False, "wp_post_id": wp_post_id, "error": str(exc)}
