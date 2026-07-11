"""LangChain tools for keyword research and analysis."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from langchain_core.tools import tool


# Common English stop words to filter out
_STOP_WORDS = {
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "as", "is", "was", "are", "were", "been",
    "be", "have", "has", "had", "do", "does", "did", "will", "would",
    "could", "should", "may", "might", "shall", "can", "this", "that",
    "these", "those", "i", "you", "he", "she", "it", "we", "they",
    "what", "which", "who", "when", "where", "why", "how", "all", "each",
    "both", "more", "also", "into", "up", "out", "so", "if", "its",
    "their", "our", "your", "his", "her", "not", "about",
}


@tool
def extract_keywords_from_content(text: str, top_n: int = 20) -> dict[str, Any]:
    """Extract the most frequent meaningful keywords from page text.

    Args:
        text: Plain text content (strip HTML first).
        top_n: Number of top keywords to return.

    Returns:
        Dict with keywords (list of {word, count, density}), total_words.
    """
    # Normalize
    text_lower = text.lower()
    words = re.findall(r"\b[a-z][a-z\-]{2,}\b", text_lower)
    filtered = [w for w in words if w not in _STOP_WORDS]
    total_words = len(words)

    # Single keywords
    counts = Counter(filtered)
    single_kws = [
        {"keyword": kw, "count": cnt, "density": round(cnt / max(total_words, 1) * 100, 2)}
        for kw, cnt in counts.most_common(top_n)
    ]

    # Bigrams
    bigrams = [f"{filtered[i]} {filtered[i+1]}" for i in range(len(filtered) - 1)]
    bigram_counts = Counter(bigrams)
    top_bigrams = [
        {"keyword": kw, "count": cnt, "density": round(cnt / max(total_words, 1) * 100, 2)}
        for kw, cnt in bigram_counts.most_common(10)
        if cnt > 1
    ]

    return {
        "top_keywords": single_kws,
        "top_bigrams": top_bigrams,
        "total_words": total_words,
    }


@tool
def analyze_keyword_density(text: str, keyword: str) -> dict[str, Any]:
    """Calculate keyword density and check if it's in the optimal range.

    Args:
        text: Plain text content.
        keyword: The focus keyword or phrase to analyze.

    Returns:
        Dict with count, density, status (optimal/too_low/too_high),
        keyword_in_first_paragraph.
    """
    words = text.split()
    total_words = len(words)
    keyword_lower = keyword.lower()
    text_lower = text.lower()

    count = text_lower.count(keyword_lower)
    density = round(count / max(total_words, 1) * 100, 2)

    if density < 0.5:
        status = "too_low"
    elif density > 3.0:
        status = "too_high"
    else:
        status = "optimal"

    # Check first ~100 words
    first_para = " ".join(words[:100]).lower()
    in_first_para = keyword_lower in first_para

    # Check in headings approximation (uppercase words or lines ending with ?)
    prominent_positions = []
    if keyword_lower in text_lower[:200]:
        prominent_positions.append("introduction")

    return {
        "keyword": keyword,
        "count": count,
        "density": density,
        "total_words": total_words,
        "status": status,
        "keyword_in_first_paragraph": in_first_para,
        "recommendation": _density_recommendation(status, density, keyword),
    }


def _density_recommendation(status: str, density: float, keyword: str) -> str:
    if status == "too_low":
        return (
            f"Increase usage of '{keyword}' — current density {density}% is below 0.5%. "
            "Add it naturally to headings, intro paragraph, and body text."
        )
    elif status == "too_high":
        return (
            f"Reduce '{keyword}' usage — {density}% density risks keyword stuffing penalty. "
            "Replace some instances with synonyms or related terms."
        )
    return f"'{keyword}' density is optimal at {density}%."


@tool
def suggest_keywords_with_llm(
    page_title: str,
    page_content_summary: str,
    existing_keywords: list[str],
    site_niche: str = "",
) -> dict[str, Any]:
    """Generate keyword suggestions based on page content (returns structured prompt).

    This tool prepares a structured analysis dict. The actual LLM call is done
    by the KeywordPlannerAgent which passes this to Claude.

    Args:
        page_title: Title of the page.
        page_content_summary: Brief summary or first 300 chars of content.
        existing_keywords: Already-identified keywords.
        site_niche: Website niche/industry for context.

    Returns:
        Dict with analysis context for the LLM to expand.
    """
    return {
        "page_title": page_title,
        "content_summary": page_content_summary[:500],
        "existing_keywords": existing_keywords[:20],
        "site_niche": site_niche,
        "analysis_request": (
            f"Based on the page '{page_title}' about '{site_niche}', "
            f"with existing keywords {existing_keywords[:5]}, "
            "suggest: 5 primary keywords, 10 long-tail keywords, "
            "3 semantic/LSI keyword clusters, and 2 content gap topics."
        ),
    }
