"""Competitive analysis agent using fixed competitor seeds (no paid SERP API).

Reads competitor URLs/domains from config and extracts lightweight page-level SEO
signals. For each target keyword, it ranks seed pages by simple keyword relevance
and returns top competitors in the same state shape consumed downstream.
"""

from __future__ import annotations

import asyncio
import re
from urllib.parse import urlparse
from typing import Any

import httpx
import structlog
from bs4 import BeautifulSoup

from src.agents.state import SEOAgentState
from src.config import get_settings

settings = get_settings()
logger = structlog.get_logger(__name__)

_MAX_KEYWORDS = 3
_MAX_COMPETITORS = 5
_FETCH_TIMEOUT = 6
_FETCH_CONCURRENCY = 3
_MAX_SEED_URLS = 15


def _normalize_seed_url(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        return ""
    if not value.startswith(("http://", "https://")):
        value = f"https://{value}"
    parsed = urlparse(value)
    if not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path or '/'}"


def _seed_urls() -> list[str]:
    urls = [_normalize_seed_url(u) for u in settings.competitor_urls]
    domains = [_normalize_seed_url(d) for d in settings.competitor_domains]
    combined = [u for u in urls + domains if u]
    seen: set[str] = set()
    result: list[str] = []
    for item in combined:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result[:_MAX_SEED_URLS]


def _keyword_tokens(keyword: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", keyword.lower()) if len(t) >= 3]


def _score_relevance(tokens: list[str], page: dict[str, Any]) -> int:
    if not tokens:
        return 0

    haystack_title = (page.get("title") or "").lower()
    haystack_h1 = (page.get("h1") or "").lower()
    haystack_h2 = " ".join(page.get("h2s", [])).lower()
    haystack_meta = (page.get("meta_description") or "").lower()
    haystack_url = (page.get("url") or "").lower()

    score = 0
    for token in tokens:
        if token in haystack_title:
            score += 4
        if token in haystack_h1:
            score += 3
        if token in haystack_h2:
            score += 2
        if token in haystack_meta:
            score += 1
        if token in haystack_url:
            score += 1
    return score


async def _extract_signals(url: str) -> dict[str, Any] | None:
    """Fetch one competitor page and extract lightweight SEO signals."""
    try:
        async with httpx.AsyncClient(
            timeout=_FETCH_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": settings.crawler_user_agent},
        ) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return None
            html = resp.text[:200_000]
    except Exception:
        return None

    soup = BeautifulSoup(html, "lxml")
    body = soup.find("body")
    text = body.get_text(" ", strip=True) if body else soup.get_text(" ", strip=True)

    h1 = (soup.find("h1") or soup.new_tag("_")).get_text(strip=True)
    h2s = [h.get_text(strip=True) for h in soup.find_all("h2")][:8]

    meta_desc_tag = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    meta_desc = meta_desc_tag.get("content", "") if meta_desc_tag else ""

    schema_types: list[str] = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            import json

            data = json.loads(script.string or "")
            if isinstance(data, dict):
                t = data.get("@type")
                if t:
                    schema_types.append(t if isinstance(t, str) else str(t))
            elif isinstance(data, list):
                schema_types.extend(item.get("@type", "") for item in data if isinstance(item, dict))
        except Exception:
            continue

    parsed = urlparse(url)
    domain = parsed.netloc
    internal_links = len(
        [
            a
            for a in soup.find_all("a", href=True)
            if a["href"].startswith("/") or domain in a["href"]
        ]
    )

    return {
        "url": url,
        "position": None,
        "title": (soup.title.string or "").strip() if soup.title else "",
        "word_count": len(text.split()),
        "h1": h1,
        "h2s": h2s,
        "meta_description": meta_desc[:200],
        "schema_types": list(set(filter(None, schema_types))),
        "internal_links": internal_links,
    }


async def serp_analysis_node(state: SEOAgentState) -> dict:
    """Analyse competitor seeds for top suggested keywords."""
    keyword_suggestions = state.get("keyword_suggestions", [])
    errors: list[str] = list(state.get("errors", []))

    if not keyword_suggestions:
        logger.info("serp_analysis.skipped", reason="no_keywords")
        return {"competitive_insights": [], "errors": errors}

    competitor_urls = _seed_urls()
    if not competitor_urls:
        logger.info("serp_analysis.skipped", reason="no_competitor_seeds")
        return {"competitive_insights": [], "errors": errors}

    ordered = sorted(
        keyword_suggestions,
        key=lambda k: {"primary": 0, "secondary": 1, "long_tail": 2}.get(
            k.get("recommendation", "long_tail"),
            2,
        ),
    )
    target_keywords = [k["keyword"] for k in ordered[:_MAX_KEYWORDS]]
    logger.info("serp_analysis.start", keywords=target_keywords, seeds=len(competitor_urls))

    semaphore = asyncio.Semaphore(_FETCH_CONCURRENCY)

    async def _fetch_with_sem(url: str) -> dict[str, Any] | None:
        async with semaphore:
            return await _extract_signals(url)

    results = await asyncio.gather(*[_fetch_with_sem(url) for url in competitor_urls], return_exceptions=True)
    pages = [r for r in results if isinstance(r, dict)]
    if not pages:
        logger.info("serp_analysis.skipped", reason="seed_fetch_failed")
        return {"competitive_insights": [], "errors": errors}

    competitive_insights: list[dict[str, Any]] = []
    for keyword in target_keywords:
        tokens = _keyword_tokens(keyword)
        scored = []
        for page in pages:
            page_copy = dict(page)
            page_copy["_score"] = _score_relevance(tokens, page)
            scored.append(page_copy)

        scored.sort(key=lambda p: (p["_score"], p.get("word_count", 0)), reverse=True)
        top = scored[:_MAX_COMPETITORS]
        for idx, comp in enumerate(top, start=1):
            comp["position"] = idx
            comp.pop("_score", None)

        if not top:
            continue

        avg_word_count = int(sum(c["word_count"] for c in top) / len(top))
        all_schema_types = list({t for c in top for t in c["schema_types"]})
        avg_h2_count = int(sum(len(c["h2s"]) for c in top) / len(top))

        competitive_insights.append(
            {
                "keyword": keyword,
                "competitors": top,
                "summary": {
                    "avg_word_count": avg_word_count,
                    "avg_h2_count": avg_h2_count,
                    "common_schemas": all_schema_types,
                    "top_competitor": top[0]["url"] if top else None,
                },
            }
        )

        logger.info(
            "serp_analysis.keyword_done",
            keyword=keyword,
            competitors=len(top),
            avg_word_count=avg_word_count,
        )

    logger.info("serp_analysis.complete", keywords_analysed=len(competitive_insights))

    print(f"################################ \nSERP Analysis complete. analysed {len(competitive_insights)} keywords with top competitors identified for each. the insights obtained is: {competitive_insights} and the obtained major errors: {errors} ################################")
    
    return {"competitive_insights": competitive_insights, "errors": errors}
