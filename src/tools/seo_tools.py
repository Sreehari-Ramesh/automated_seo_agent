"""LangChain tools for SEO analysis of HTML pages."""

from __future__ import annotations

import json
import re
from typing import Any

from bs4 import BeautifulSoup
from langchain_core.tools import tool

from src.config import get_settings

settings = get_settings()


@tool
def analyze_meta_tags(html: str, url: str = "") -> dict[str, Any]:
    """Analyze all meta/SEO tags in a page's HTML.

    Args:
        html: The full HTML source of the page.
        url: Optional page URL for context.

    Returns:
        Dict with title, meta_description, og_title, og_description,
        og_image, canonical, robots_meta, issues list.
    """
    soup = BeautifulSoup(html, "lxml")
    
    issues: list[str] = []

    # Title
    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else ""
    if not title:
        issues.append("MISSING_TITLE: Page has no <title> tag")
    elif len(title) < settings.seo_title_min:
        issues.append(f"TITLE_TOO_SHORT: Title is {len(title)} chars (min {settings.seo_title_min})")
    elif len(title) > settings.seo_title_max:
        issues.append(f"TITLE_TOO_LONG: Title is {len(title)} chars (max {settings.seo_title_max})")

    # Meta description
    desc_tag = soup.find("meta", attrs={"name": "description"})
    desc = desc_tag.get("content", "").strip() if desc_tag else ""
    if not desc:
        issues.append("MISSING_META_DESC: No meta description found")
    elif len(desc) < settings.seo_desc_min:
        issues.append(f"META_DESC_TOO_SHORT: {len(desc)} chars (min {settings.seo_desc_min})")
    elif len(desc) > settings.seo_desc_max:
        issues.append(f"META_DESC_TOO_LONG: {len(desc)} chars (max {settings.seo_desc_max})")

    # Open Graph
    def _og(prop: str) -> str:
        tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"property": prop})
        return tag.get("content", "").strip() if tag else ""

    og_title = _og("og:title")
    og_desc = _og("og:description")
    og_image = _og("og:image")
    if not og_title:
        issues.append("MISSING_OG_TITLE: No og:title meta tag")
    if not og_desc:
        issues.append("MISSING_OG_DESC: No og:description meta tag")
    if not og_image:
        issues.append("MISSING_OG_IMAGE: No og:image meta tag")

    # Canonical
    canonical_tag = soup.find("link", rel="canonical")
    canonical = canonical_tag.get("href", "").strip() if canonical_tag else ""
    if not canonical:
        issues.append("MISSING_CANONICAL: No canonical link tag")

    # Robots meta
    robots_tag = soup.find("meta", attrs={"name": re.compile(r"^robots$", re.I)})
    robots_meta = robots_tag.get("content", "").strip() if robots_tag else ""
    if "noindex" in robots_meta.lower():
        issues.append("NOINDEX_SET: Page has noindex directive")

    return {
        "title": title,
        "title_length": len(title),
        "meta_description": desc,
        "meta_desc_length": len(desc),
        "og_title": og_title,
        "og_description": og_desc,
        "og_image": og_image,
        "canonical": canonical,
        "robots_meta": robots_meta,
        "issues": issues,
    }


@tool
def analyze_headings(html: str) -> dict[str, Any]:
    """Analyze H1–H6 heading structure in a page.

    Args:
        html: Full HTML source.

    Returns:
        Dict with h1_count, h1_texts, h2_count, h2_texts,
        heading_tree, issues.
    """
    soup = BeautifulSoup(html, "lxml")
    issues: list[str] = []

    headings: dict[str, list[str]] = {}
    for level in range(1, 7):
        tags = soup.find_all(f"h{level}")
        headings[f"h{level}"] = [t.get_text(strip=True) for t in tags]

    h1s = headings["h1"]
    if not h1s:
        issues.append("MISSING_H1: Page has no H1 heading")
    elif len(h1s) > 1:
        issues.append(f"MULTIPLE_H1: Page has {len(h1s)} H1 headings (should have exactly 1)")

    if not headings["h2"]:
        issues.append("MISSING_H2: No H2 subheadings found — content may lack structure")

    # Check heading order (H3 without H2, etc.)
    prev_level = 0
    for level in range(1, 7):
        if headings[f"h{level}"] and prev_level and level > prev_level + 1:
            issues.append(
                f"HEADING_SKIP: H{level} found but H{prev_level + 1} is missing (skip in hierarchy)"
            )
        if headings[f"h{level}"]:
            prev_level = level

    return {
        "h1_count": len(h1s),
        "h1_texts": h1s,
        "h2_count": len(headings["h2"]),
        "h2_texts": headings["h2"][:10],
        "h3_count": len(headings["h3"]),
        "heading_structure": {k: v for k, v in headings.items() if v},
        "issues": issues,
    }


@tool
def analyze_content_quality(html: str, focus_keyword: str = "") -> dict[str, Any]:
    """Analyze page content quality, readability, and keyword usage.

    Args:
        html: Full HTML source.
        focus_keyword: Primary keyword to check density for.

    Returns:
        Dict with word_count, paragraph_count, keyword_density,
        readability_level, thin_content, issues.
    """
    soup = BeautifulSoup(html, "lxml")
    issues: list[str] = []

    # Remove script/style/nav/header/footer noise
    for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
        tag.decompose()

    text = soup.get_text(separator=" ", strip=True)
    words = [w for w in re.split(r"\s+", text) if len(w) > 1]
    word_count = len(words)
    paragraphs = soup.find_all("p")

    if word_count < settings.seo_min_word_count:
        issues.append(
            f"THIN_CONTENT: Only {word_count} words (minimum {settings.seo_min_word_count})"
        )

    if len(paragraphs) < 3:
        issues.append("LOW_PARAGRAPH_COUNT: Very few paragraphs — content may lack depth")

    # Keyword density
    keyword_density = 0.0
    if focus_keyword and word_count > 0:
        kw_lower = focus_keyword.lower()
        text_lower = text.lower()
        count = text_lower.count(kw_lower)
        keyword_density = round(count / word_count * 100, 2)
        if keyword_density < 0.5:
            issues.append(
                f"LOW_KEYWORD_DENSITY: '{focus_keyword}' appears {count}x ({keyword_density}%) — too low"
            )
        elif keyword_density > 3.0:
            issues.append(
                f"KEYWORD_STUFFING: '{focus_keyword}' at {keyword_density}% density — reduce usage"
            )

    # Basic readability (Flesch-Kincaid approximation)
    sentences = re.split(r"[.!?]+", text)
    sentences = [s.strip() for s in sentences if len(s.strip()) > 10]
    avg_sentence_len = word_count / max(len(sentences), 1)
    if avg_sentence_len > 25:
        issues.append(
            f"LONG_SENTENCES: Average sentence length is {avg_sentence_len:.1f} words — aim for <20"
        )

    # Internal / external link ratio
    all_links = soup.find_all("a", href=True)
    internal_count = sum(1 for a in all_links if not a["href"].startswith("http"))
    external_count = len(all_links) - internal_count
    if internal_count == 0 and word_count > 200:
        issues.append("NO_INTERNAL_LINKS: No internal links found — add internal linking")

    return {
        "word_count": word_count,
        "paragraph_count": len(paragraphs),
        "sentence_count": len(sentences),
        "avg_sentence_length": round(avg_sentence_len, 1),
        "keyword_density": keyword_density,
        "internal_links": internal_count,
        "external_links": external_count,
        "thin_content": word_count < settings.seo_min_word_count,
        "issues": issues,
    }


@tool
def analyze_images(html: str) -> dict[str, Any]:
    """Audit images for SEO best practices (alt text, dimensions, lazy loading).

    Args:
        html: Full HTML source.

    Returns:
        Dict with total_images, images_without_alt, images_without_dimensions,
        images_not_lazy, issues.
    """
    soup = BeautifulSoup(html, "lxml")
    images = soup.find_all("img")
    issues: list[str] = []

    without_alt = [img.get("src", "") for img in images if not img.get("alt")]
    without_dims = [
        img.get("src", "")
        for img in images
        if not img.get("width") and not img.get("height")
    ]
    not_lazy = [
        img.get("src", "")
        for img in images
        if img.get("loading") != "lazy"
    ]

    if without_alt:
        issues.append(
            f"IMAGES_MISSING_ALT: {len(without_alt)} image(s) have no alt text"
        )
    if not_lazy and len(not_lazy) > 3:
        issues.append(
            f"IMAGES_NOT_LAZY: {len(not_lazy)} image(s) not using lazy loading"
        )

    return {
        "total_images": len(images),
        "images_without_alt": len(without_alt),
        "images_without_alt_srcs": without_alt[:5],
        "images_without_dimensions": len(without_dims),
        "images_not_lazy": len(not_lazy),
        "issues": issues,
    }


@tool
def check_schema_markup(html: str) -> dict[str, Any]:
    """Check for JSON-LD structured data / Schema.org markup.

    Args:
        html: Full HTML source.

    Returns:
        Dict with schemas_found, schema_types, valid_json, issues.
    """
    soup = BeautifulSoup(html, "lxml")
    issues: list[str] = []
    schemas: list[dict] = []

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            schemas.append(data)
        except (json.JSONDecodeError, TypeError):
            issues.append("INVALID_SCHEMA: Found malformed JSON-LD script tag")

    if not schemas:
        issues.append("MISSING_SCHEMA: No JSON-LD structured data found")

    schema_types = []
    for s in schemas:
        if isinstance(s, dict):
            t = s.get("@type") or s.get("@graph", [{}])[0].get("@type", "Unknown")
            schema_types.append(t)
        elif isinstance(s, list):
            schema_types.extend([x.get("@type", "Unknown") for x in s if isinstance(x, dict)])

    return {
        "schemas_found": len(schemas),
        "schema_types": schema_types,
        "schemas": schemas[:3],  # Return first 3 for context
        "issues": issues,
    }


@tool
def check_canonical(html: str, page_url: str = "") -> dict[str, Any]:
    """Check canonical URL configuration and potential duplicate content issues.

    Args:
        html: Full HTML source.
        page_url: The actual URL of this page (for self-referencing check).

    Returns:
        Dict with canonical_url, is_self_referencing, issues.
    """
    soup = BeautifulSoup(html, "lxml")
    issues: list[str] = []

    canonical_tag = soup.find("link", rel="canonical")
    canonical = canonical_tag.get("href", "").strip() if canonical_tag else ""

    is_self = False
    if not canonical:
        issues.append("MISSING_CANONICAL: No canonical link element found")
    elif page_url and canonical != page_url:
        # Could be pointing to a different URL (pagination, translated version, etc.)
        is_self = False
        issues.append(
            f"CANONICAL_MISMATCH: Canonical '{canonical}' differs from page URL '{page_url}'"
        )
    else:
        is_self = True

    # Check for multiple canonical tags
    all_canonical = soup.find_all("link", rel="canonical")
    if len(all_canonical) > 1:
        issues.append(f"MULTIPLE_CANONICAL: {len(all_canonical)} canonical tags found — keep only one")

    return {
        "canonical_url": canonical,
        "is_self_referencing": is_self,
        "canonical_count": len(all_canonical),
        "issues": issues,
    }
