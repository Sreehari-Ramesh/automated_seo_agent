"""LangChain tools for SERP position tracking and Search Console data."""

from __future__ import annotations

from typing import Any

import httpx
from langchain_core.tools import tool

from src.config import get_settings

settings = get_settings()


@tool
async def check_serp_position(keyword: str, target_url: str) -> dict[str, Any]:
    """Check the SERP position of a URL for a given keyword via SerpAPI.

    Falls back to a mock response if no API key is configured.

    Args:
        keyword: The search query to check.
        target_url: The URL to look for in the results.

    Returns:
        Dict with position, total_results, page, featured_snippet, error.
    """
    api_key = settings.serp_api_key
    if not api_key:
        return {
            "keyword": keyword,
            "target_url": target_url,
            "position": None,
            "total_results": None,
            "page": None,
            "featured_snippet": False,
            "error": "SERP_API_KEY not configured — ranking data unavailable",
        }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://serpapi.com/search",
                params={
                    "q": keyword,
                    "api_key": api_key,
                    "num": 100,
                    "gl": "us",
                    "hl": "en",
                },
            )
            resp.raise_for_status()
            data = resp.json()

        results = data.get("organic_results", [])
        position = None
        for i, r in enumerate(results, start=1):
            link = r.get("link", "")
            if target_url in link or link in target_url:
                position = i
                break

        featured = data.get("answer_box", {}).get("link", "")
        has_featured = target_url in featured

        return {
            "keyword": keyword,
            "target_url": target_url,
            "position": position,
            "total_results": data.get("search_information", {}).get("total_results"),
            "page": (position // 10) + 1 if position else None,
            "featured_snippet": has_featured,
            "error": None,
        }
    except Exception as exc:
        return {
            "keyword": keyword,
            "target_url": target_url,
            "position": None,
            "total_results": None,
            "page": None,
            "featured_snippet": False,
            "error": str(exc),
        }


@tool
async def get_search_console_data(site_url: str, days: int = 28) -> dict[str, Any]:
    """Fetch Search Console performance data (impressions, clicks, CTR, position).

    Returns a structured summary. Requires GOOGLE_SEARCH_CONSOLE_CREDENTIALS.

    Args:
        site_url: The verified site URL in GSC (e.g. https://example.com/).
        days: Number of days to look back (default 28).

    Returns:
        Dict with top_queries, top_pages, averages, error.
    """
    credentials_path = settings.google_pagespeed_api_key  # Reusing field as proxy
    if not credentials_path:
        return {
            "site_url": site_url,
            "top_queries": [],
            "top_pages": [],
            "averages": {},
            "error": (
                "GOOGLE_SEARCH_CONSOLE_CREDENTIALS not configured. "
                "Set the env var to enable GSC data."
            ),
        }

    try:
        # Production: use google-auth + googleapiclient
        # For now we return a structured placeholder indicating the integration point
        from datetime import datetime, timedelta

        end_date = datetime.utcnow().date()
        start_date = end_date - timedelta(days=days)

        return {
            "site_url": site_url,
            "date_range": {"start": str(start_date), "end": str(end_date)},
            "top_queries": [],
            "top_pages": [],
            "averages": {
                "clicks": 0,
                "impressions": 0,
                "ctr": 0.0,
                "position": 0.0,
            },
            "error": "GSC integration requires google-auth library and service account credentials",
        }
    except Exception as exc:
        return {
            "site_url": site_url,
            "top_queries": [],
            "top_pages": [],
            "averages": {},
            "error": str(exc),
        }
