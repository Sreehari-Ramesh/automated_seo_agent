"""WordPress REST API v2 client with Yoast SEO support."""

from __future__ import annotations

import base64
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from src.config import get_settings

settings = get_settings()


class WordPressClient:
    """Async client for the WordPress REST API v2.

    Supports:
    - Listing / reading posts and pages
    - Updating post content, title, meta
    - Creating new posts / pages
    - Reading & writing Yoast SEO meta (via post meta endpoint)
    - Snapshot capture for rollback
    """

    def __init__(
        self,
        base_url: str | None = None,
        username: str | None = None,
        app_password: str | None = None,
    ) -> None:
        self.base_url = (base_url or settings.wp_base_url).strip().rstrip("/")
        username = username or settings.wp_username
        app_password = app_password or settings.wp_app_password
        token = base64.b64encode(f"{username}:{app_password}".encode()).decode()
        self._headers = {
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
            "User-Agent": settings.crawler_user_agent,
        }
        self._api = f"{self.base_url}/wp-json/wp/v2"
        self._timeout = settings.wp_request_timeout

    # ── Internal helpers ─────────────────────────────────────────────────────

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def _get(self, path: str, params: dict | None = None) -> Any:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.get(
                f"{self._api}{path}", headers=self._headers, params=params or {}
            )
            r.raise_for_status()
            return r.json()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def _post(self, path: str, data: dict) -> Any:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.post(
                f"{self._api}{path}", headers=self._headers, json=data
            )
            r.raise_for_status()
            return r.json()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def _put(self, path: str, data: dict) -> Any:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.put(
                f"{self._api}{path}", headers=self._headers, json=data
            )
            r.raise_for_status()
            return r.json()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def _options(self, path: str) -> Any:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.request("OPTIONS", f"{self._api}{path}", headers=self._headers)
            r.raise_for_status()
            return r.json()

    # ── Read operations ───────────────────────────────────────────────────────

    async def list_posts(self, per_page: int = 100, page: int = 1) -> list[dict]:
        """Return a page of posts with meta fields exposed."""
        return await self._get(
            "/posts",
            params={"per_page": per_page, "page": page, "status": "publish", "_fields": _POST_FIELDS},
        )

    async def list_pages(self, per_page: int = 100, page: int = 1) -> list[dict]:
        return await self._get(
            "/pages",
            params={"per_page": per_page, "page": page, "status": "publish", "_fields": _POST_FIELDS},
        )

    async def get_post(self, post_id: int) -> dict:
        return await self._get(f"/posts/{post_id}", params={"_fields": _POST_FIELDS})

    async def get_page(self, page_id: int) -> dict:
        return await self._get(f"/pages/{page_id}", params={"_fields": _POST_FIELDS})

    async def get_all_posts_and_pages(self) -> list[dict]:
        """Fetch all published posts + pages (handles pagination)."""
        results: list[dict] = []
        for endpoint in ("list_posts", "list_pages"):
            fn = getattr(self, endpoint)
            p = 1
            while True:
                batch = await fn(per_page=100, page=p)
                if not batch:
                    break
                results.extend(batch)
                if len(batch) < 100:
                    break
                p += 1
        return results

    # ── Write operations ──────────────────────────────────────────────────────

    async def update_post_meta(self, post_id: int, meta: dict[str, str]) -> dict:
        """Update Yoast SEO meta fields on a post/page.

        Common Yoast meta keys:
          _yoast_wpseo_title        → SEO title
          _yoast_wpseo_metadesc     → Meta description
          _yoast_wpseo_focuskw      → Focus keyword
          _yoast_wpseo_canonical    → Canonical URL
        """
        return await self._put(f"/posts/{post_id}", {"meta": meta})

    async def update_page_meta(self, page_id: int, meta: dict[str, str]) -> dict:
        return await self._put(f"/pages/{page_id}", {"meta": meta})

    async def update_post_content(
        self, post_id: int, content: str, title: str | None = None
    ) -> dict:
        payload: dict[str, Any] = {"content": content}
        if title:
            payload["title"] = title
        return await self._put(f"/posts/{post_id}", payload)

    async def update_page_content(
        self, page_id: int, content: str, title: str | None = None
    ) -> dict:
        payload: dict[str, Any] = {"content": content}
        if title:
            payload["title"] = title
        return await self._put(f"/pages/{page_id}", payload)

    async def create_post(
        self,
        title: str,
        content: str,
        slug: str | None = None,
        categories: list[int] | None = None,
        meta: dict | None = None,
        status: str = "draft",
    ) -> dict:
        payload: dict[str, Any] = {
            "title": title,
            "content": content,
            "status": status,
        }
        if slug:
            payload["slug"] = slug
        if categories:
            payload["categories"] = categories
        if meta:
            payload["meta"] = meta
        return await self._post("/posts", payload)

    async def create_page(
        self,
        title: str,
        content: str,
        slug: str | None = None,
        parent: int | None = None,
        meta: dict | None = None,
        status: str = "draft",
    ) -> dict:
        payload: dict[str, Any] = {
            "title": title,
            "content": content,
            "status": status,
        }
        if slug:
            payload["slug"] = slug
        if parent:
            payload["parent"] = parent
        if meta:
            payload["meta"] = meta
        return await self._post("/pages", payload)

    # ── Snapshot helpers ──────────────────────────────────────────────────────

    async def snapshot_post(self, post_id: int, post_type: str = "post") -> dict:
        """Capture current SEO state for rollback."""
        if post_type == "page":
            data = await self.get_page(post_id)
        else:
            data = await self.get_post(post_id)
        return {
            "wp_post_id": post_id,
            "post_type": post_type,
            "title": data.get("title", {}).get("rendered", ""),
            "content": data.get("content", {}).get("rendered", ""),
            "yoast_title": data.get("yoast_head_json", {}).get("title", ""),
            "yoast_desc": data.get("yoast_head_json", {}).get("description", ""),
            "meta": data.get("meta", {}),
        }

    async def restore_snapshot(self, post_id: int, snapshot: dict) -> dict:
        """Restore a post to its snapshotted state."""
        post_type = snapshot.get("post_type", "post")
        meta = snapshot.get("meta", {})
        content = snapshot.get("content", "")
        title = snapshot.get("title", "")
        if post_type == "page":
            result = await self.update_page_content(post_id, content, title)
            await self.update_page_meta(post_id, meta)
        else:
            result = await self.update_post_content(post_id, content, title)
            await self.update_post_meta(post_id, meta)
        return result

    # ── Site health ───────────────────────────────────────────────────────────

    async def check_connection(self) -> bool:
        """Test that credentials work."""
        try:
            result = await self._get("/users/me", params={"_fields": "id,name"})
            return bool(result.get("id"))
        except Exception:
            return False

    async def check_ai_meta_support(self, post_id: int, post_type: str = "post") -> dict:
        """Check whether seo-agent-meta.php exposed ai_* fields in REST."""
        path = f"/pages/{post_id}" if post_type == "page" else f"/posts/{post_id}"
        expected = {
            "ai_meta_title",
            "ai_meta_description",
            "ai_canonical_url",
            "ai_og_title",
            "ai_og_description",
            "ai_og_image",
            "ai_schema_json",
            "ai_focus_keyword",
        }
        try:
            data = await self._options(path)
            meta_props = (
                data.get("schema", {})
                .get("properties", {})
                .get("meta", {})
                .get("properties", {})
            )
            available = set(meta_props.keys())
            missing = sorted(expected - available)
            return {
                "supported": not missing,
                "missing_fields": missing,
                "error": None,
            }
        except Exception as exc:
            return {
                "supported": False,
                "missing_fields": sorted(expected),
                "error": str(exc),
            }

    async def get_site_info(self) -> dict:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.get(
                f"{self.base_url}/wp-json/",
                headers=self._headers,
            )
            r.raise_for_status()
            return r.json()


# Fields requested from WP API — includes Yoast SEO head JSON
_POST_FIELDS = (
    "id,date,modified,slug,status,type,link,title,content,excerpt,"
    "author,featured_media,categories,tags,meta,yoast_head_json"
)
