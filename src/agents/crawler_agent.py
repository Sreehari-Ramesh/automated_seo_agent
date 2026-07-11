"""Crawler Agent — discovers and fetches all pages on the target WordPress site."""

from __future__ import annotations

import asyncio
import time
from urllib.parse import urlparse

import structlog
from langchain_core.messages import HumanMessage

from src.agents.state import CrawledPage, SEOAgentState
from src.config import get_settings
from src.tools.crawler_tools import (
    check_robots_txt,
    check_page_speed,
    fetch_page,
    fetch_sitemap_urls,
)
from src.wordpress.client import WordPressClient

settings = get_settings()
logger = structlog.get_logger(__name__)


# ── Node function ─────────────────────────────────────────────────────────────

async def crawler_node(state: SEOAgentState) -> dict:
    """LangGraph node: crawls the WordPress site and populates crawled_pages."""

    # If crawled_pages are already injected (e.g. run_single_page.py), skip the
    # full crawl and pass them through. Production runs start with crawled_pages=[]
    # so this block is never hit in normal operation.
    if state.get("crawled_pages"):
        logger.info(
            "crawler_agent.skip",
            reason="crawled_pages already populated",
            pages=len(state["crawled_pages"]),
        )
        return {
            "crawled_pages": state["crawled_pages"],
            "crawl_stats": state.get("crawl_stats", {"pages_crawled": len(state["crawled_pages"])}),
            "errors": list(state.get("errors", [])),
        }

    logger.info("crawler_agent.start", target_url=state["target_url"])
    start_time = time.monotonic()
    errors: list[str] = list(state.get("errors", []))
    base_url = state["target_url"]

    try:
        # ── Step 1: WordPress REST API — authoritative page list ──────────────
        wp_client = WordPressClient()
        wp_content = await wp_client.get_all_posts_and_pages()

        # ── Step 2: Direct tool calls — robots.txt + sitemaps + page speed ───
        # No LLM needed: the sequence is deterministic, calling through the LLM
        # only accumulates large tool-result blobs as context, wasting input tokens.
        robots_result: dict = {}
        sitemap_urls: list[str] = []

        try:
            raw_robots = await check_robots_txt.ainvoke({"base_url": base_url})
            if isinstance(raw_robots, dict):
                robots_result = raw_robots
        except Exception as exc:
            errors.append(f"robots.txt fetch error: {exc}")

        sitemap_candidates: list[str] = robots_result.get("sitemap_urls", [])
        if not sitemap_candidates:
            sitemap_candidates = [f"{base_url.rstrip('/')}/sitemap.xml"]

        for sm_url in sitemap_candidates:
            try:
                sm_result = await fetch_sitemap_urls.ainvoke({"sitemap_url": sm_url})
                if isinstance(sm_result, dict):
                    sitemap_urls.extend(sm_result.get("urls", []))
            except Exception as exc:
                errors.append(f"sitemap fetch error ({sm_url}): {exc}")

        try:
            await check_page_speed.ainvoke({"url": base_url})
        except Exception as exc:
            errors.append(f"page speed check error: {exc}")

        # ── Step 3: Concurrent page fetching ─────────────────────────────────
        wp_urls = {item.get("link", "") for item in wp_content if item.get("link")}

        #########need to uncomment this later###############3
        all_urls = [base_url]
        # all_urls = list(wp_urls | set(sitemap_urls))
        all_urls = list((wp_urls | set(sitemap_urls)) | {base_url})


        origin = f"{urlparse(base_url).scheme}://{urlparse(base_url).netloc}"
        all_urls = [u for u in all_urls if u.startswith(origin)]

        # state max_pages overrides global setting: None=use setting, 0=full site, N=limit
        state_max = state.get("max_pages")
        if state_max is None:
            max_pages = settings.wp_max_pages
        else:
            max_pages = state_max
        if max_pages > 0:
            all_urls = all_urls[:max_pages]

        sem = asyncio.Semaphore(settings.crawler_concurrency)

        async def _fetch_one(url: str) -> dict:
            async with sem:
                result = await fetch_page.ainvoke({"url": url})
                await asyncio.sleep(settings.crawler_delay)
                return result

        fetch_results = await asyncio.gather(
            *[_fetch_one(url) for url in all_urls], return_exceptions=True
        )

        # ── Step 4: Build CrawledPage objects ─────────────────────────────────
        wp_map: dict[str, dict] = {}
        for item in wp_content:
            link = item.get("link", "").rstrip("/")
            if link:
                wp_map[link] = item

        crawled_pages: list[CrawledPage] = []
        pages_failed = 0

        for i, result in enumerate(fetch_results):
            url = all_urls[i] if i < len(all_urls) else ""
            if isinstance(result, Exception):
                pages_failed += 1
                errors.append(f"Crawl error for {url}: {result}")
                continue

            fetched: dict = result
            if fetched.get("error"):
                pages_failed += 1
                errors.append(f"Crawl error for {url}: {fetched['error']}")

            normalized_url = fetched.get("url", url).rstrip("/")
            wp_item = wp_map.get(normalized_url, {})
            word_count = len(fetched.get("text", "").split())

            page: CrawledPage = {
                "url": fetched.get("url", url),
                "wp_post_id": wp_item.get("id"),
                "post_type": wp_item.get("type", "page"),
                "title": (
                    # wp_item.get("title", {}).get("rendered", "")
                    # or fetched.get("title", "")
                    fetched.get("title", "")
                ),
                "html": fetched.get("html", ""),
                "text": fetched.get("text", ""),
                "links": fetched.get("links", []),
                "status_code": fetched.get("status_code", 0),
                "load_time_ms": fetched.get("load_time_ms", 0),
                "word_count": word_count,
                "error": fetched.get("error"),
            }
            crawled_pages.append(page)

        duration = round(time.monotonic() - start_time, 2)
        pages_crawled = len(crawled_pages)

        crawl_summary = {
            "total_wp_content": len(wp_content),
            "pages_attempted": len(all_urls),
            "pages_crawled": pages_crawled,
            "pages_failed": pages_failed,
            "sitemap_urls_found": len(sitemap_urls),
            "robots_disallowed": len(robots_result.get("disallowed_paths", [])),
            "avg_load_time_ms": int(
                sum(p["load_time_ms"] for p in crawled_pages) / max(pages_crawled, 1)
            ),
            "slow_pages": [p["url"] for p in crawled_pages if p["load_time_ms"] > 3000],
            "error_pages": [p["url"] for p in crawled_pages if p.get("error")],
            "duration_s": duration,
        }

        logger.info(
            "crawler_agent.complete",
            pages_crawled=pages_crawled,
            pages_failed=pages_failed,
            duration_s=duration,
        )

        return {
            "crawled_pages": crawled_pages,
            "crawl_stats": crawl_summary,
            "errors": errors,
            "messages": [
                HumanMessage(
                    content=f"Crawler completed: {pages_crawled} pages crawled in {duration}s. "
                    f"{pages_failed} failed. Avg load time: {crawl_summary['avg_load_time_ms']}ms."
                )
            ],
        }

    except Exception as exc:
        logger.error("crawler_agent.error", error=str(exc))
        errors.append(f"Crawler agent fatal error: {exc}")
        return {
            "crawled_pages": [],
            "crawl_stats": {},
            "errors": errors,
        }
