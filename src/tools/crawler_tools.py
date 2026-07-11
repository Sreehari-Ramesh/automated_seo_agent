"""LangChain tools for web crawling and page fetching."""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup
from langchain_core.tools import tool

from src.config import get_settings

settings = get_settings()

_NAMESPACE = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


@tool
async def fetch_page(url: str) -> dict[str, Any]:
    """Fetch a webpage and return its content, links, status, and timing.

    Args:
        url: The full URL to fetch.

    Returns:
        Dict with keys: url, status_code, title, html, text, links,
        load_time_ms, error.
    """
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(
            timeout=20,
            follow_redirects=True,
            headers={"User-Agent": settings.crawler_user_agent},
        ) as client:
            resp = await client.get(url)
            load_ms = int((time.monotonic() - start) * 1000)
            html = resp.text
            soup = BeautifulSoup(html, "lxml")

            # Extract all internal/external links
            base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
            links: list[str] = []
            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                if href.startswith("http"):
                    links.append(href)
                elif href.startswith("/"):
                    links.append(urljoin(base, href))

            title_tag = soup.find("title")
            return {
                "url": str(resp.url),
                "status_code": resp.status_code,
                "title": title_tag.get_text(strip=True) if title_tag else "",
                "html": html,                          # kept for CrawledPage build
                "text": soup.get_text(separator=" ", strip=True)[:2000],  # summary only
                "links": list(set(links))[:50],        # cap link list
                "load_time_ms": load_ms,
                "error": None,
            }
    except Exception as exc:
        return {
            "url": url,
            "status_code": 0,
            "title": "",
            "html": "",
            "text": "",
            "links": [],
            "load_time_ms": int((time.monotonic() - start) * 1000),
            "error": str(exc),
        }


@tool
async def check_robots_txt(base_url: str) -> dict[str, Any]:
    """Fetch and parse robots.txt for a site.

    Args:
        base_url: The root URL of the site (e.g. https://example.com).

    Returns:
        Dict with keys: robots_url, crawl_delay, disallowed_paths,
        sitemap_urls, error.
    """
    robots_url = base_url.rstrip("/") + "/robots.txt"
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.get(robots_url)
        content = resp.text if resp.status_code == 200 else ""

        rp = RobotFileParser()
        rp.set_url(robots_url)
        rp.read()

        disallowed: list[str] = []
        sitemaps: list[str] = []
        delay: float | None = None

        for line in content.splitlines():
            line = line.strip()
            if line.lower().startswith("disallow:"):
                path = line.split(":", 1)[1].strip()
                if path:
                    disallowed.append(path)
            elif line.lower().startswith("sitemap:"):
                sitemaps.append(line.split(":", 1)[1].strip())
            elif line.lower().startswith("crawl-delay:"):
                try:
                    delay = float(line.split(":", 1)[1].strip())
                except ValueError:
                    pass

        return {
            "robots_url": robots_url,
            "crawl_delay": delay,
            "disallowed_paths": disallowed,
            "sitemap_urls": sitemaps,
            "error": None,
        }
    except Exception as exc:
        return {
            "robots_url": robots_url,
            "crawl_delay": None,
            "disallowed_paths": [],
            "sitemap_urls": [],
            "error": str(exc),
        }


@tool
async def fetch_sitemap_urls(sitemap_url: str) -> dict[str, Any]:
    """Fetch and parse an XML sitemap (or sitemap index).

    Args:
        sitemap_url: URL to the sitemap.xml file.

    Returns:
        Dict with keys: urls (list of page URLs), sitemap_count, error.
    """
    try:
        async with httpx.AsyncClient(
            timeout=20,
            follow_redirects=True,
            headers={"User-Agent": settings.crawler_user_agent},
        ) as client:
            resp = await client.get(sitemap_url)
            resp.raise_for_status()
        root = ET.fromstring(resp.text)

        urls: list[str] = []
        # Sitemap index
        for sm in root.findall("sm:sitemap/sm:loc", _NAMESPACE):
            if sm.text:
                child = await fetch_sitemap_urls.ainvoke({"sitemap_url": sm.text})
                urls.extend(child.get("urls", []))
        # Regular sitemap
        for url_el in root.findall("sm:url/sm:loc", _NAMESPACE):
            if url_el.text:
                urls.append(url_el.text.strip())

        return {"urls": urls, "sitemap_count": len(urls), "error": None}
    except Exception as exc:
        return {"urls": [], "sitemap_count": 0, "error": str(exc)}


@tool
async def check_page_speed(url: str) -> dict[str, Any]:
    """Run Google PageSpeed Insights and return Core Web Vitals.

    Falls back to a basic HTTP timing if no API key is configured.

    Args:
        url: The page URL to test.

    Returns:
        Dict with keys: performance_score, lcp, fid, cls, fcp, ttfb,
        mobile_friendly, error.
    """
    api_key = settings.google_pagespeed_api_key
    if api_key:
        try:
            psi_url = (
                "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
                f"?url={url}&key={api_key}&strategy=mobile"
                "&category=performance&category=accessibility&category=seo"
            )
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(psi_url)
                resp.raise_for_status()
                data = resp.json()

            lhr = data.get("lighthouseResult", {})
            cats = lhr.get("categories", {})
            audits = lhr.get("audits", {})

            def _num(key: str) -> float | None:
                v = audits.get(key, {}).get("numericValue")
                return round(v, 2) if v is not None else None

            return {
                "performance_score": round(
                    (cats.get("performance", {}).get("score") or 0) * 100
                ),
                "seo_score": round(
                    (cats.get("seo", {}).get("score") or 0) * 100
                ),
                "accessibility_score": round(
                    (cats.get("accessibility", {}).get("score") or 0) * 100
                ),
                "lcp": _num("largest-contentful-paint"),
                "fid": _num("max-potential-fid"),
                "cls": _num("cumulative-layout-shift"),
                "fcp": _num("first-contentful-paint"),
                "ttfb": _num("server-response-time"),
                "mobile_friendly": True,
                "error": None,
            }
        except Exception as exc:
            return _basic_speed_fallback(url, str(exc))
    else:
        return await _basic_speed_fallback_async(url)


async def _basic_speed_fallback_async(url: str) -> dict[str, Any]:
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(url)
        load_ms = int((time.monotonic() - start) * 1000)
        score = max(0, 100 - load_ms // 50)
        return {
            "performance_score": score,
            "seo_score": None,
            "accessibility_score": None,
            "lcp": None,
            "fid": None,
            "cls": None,
            "fcp": None,
            "ttfb": load_ms,
            "mobile_friendly": None,
            "error": "No API key — basic timing only",
        }
    except Exception as exc:
        return {
            "performance_score": 0,
            "seo_score": None,
            "accessibility_score": None,
            "lcp": None,
            "fid": None,
            "cls": None,
            "fcp": None,
            "ttfb": None,
            "mobile_friendly": None,
            "error": str(exc),
        }


def _basic_speed_fallback(url: str, err: str) -> dict[str, Any]:
    return {
        "performance_score": 0,
        "seo_score": None,
        "accessibility_score": None,
        "lcp": None,
        "fid": None,
        "cls": None,
        "fcp": None,
        "ttfb": None,
        "mobile_friendly": None,
        "error": err,
    }
