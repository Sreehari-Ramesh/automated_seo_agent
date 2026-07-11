"""Tests for keyword analysis tools."""

import pytest
from src.tools.keyword_tools import extract_keywords_from_content, analyze_keyword_density


def test_extract_keywords():
    text = "The best plumber in London provides emergency plumbing services. Expert plumbers London available 24/7 for plumbing repairs and boiler services."
    result = extract_keywords_from_content.invoke({"text": text, "top_n": 10})
    keywords = [k["keyword"] for k in result["top_keywords"]]
    assert "plumber" in keywords or "plumbing" in keywords or "london" in keywords
    assert result["total_words"] > 0


def test_keyword_density_optimal():
    text = "plumber " * 5 + "other words " * 90
    result = analyze_keyword_density.invoke({"text": text, "keyword": "plumber"})
    assert result["count"] == 5
    assert result["status"] in ("optimal", "too_low")


def test_keyword_density_stuffing():
    text = "plumber " * 40 + "other words " * 10
    result = analyze_keyword_density.invoke({"text": text, "keyword": "plumber"})
    assert result["status"] == "too_high"


def test_keyword_density_low():
    text = "some generic content about various topics without any specific keyword mention here at all"
    result = analyze_keyword_density.invoke({"text": text, "keyword": "plumber"})
    assert result["status"] == "too_low"
    assert result["count"] == 0
