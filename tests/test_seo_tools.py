"""Tests for SEO analysis tools."""

import pytest
from src.tools.seo_tools import (
    analyze_meta_tags,
    analyze_headings,
    analyze_content_quality,
    analyze_images,
    check_schema_markup,
    check_canonical,
)

SAMPLE_HTML_GOOD = """
<!DOCTYPE html>
<html>
<head>
  <title>Best Plumber in London | Expert Plumbing Services</title>
  <meta name="description" content="Looking for a reliable plumber in London? Our expert team offers 24/7 emergency plumbing, boiler repairs, and bathroom installations. Call us today for a free quote.">
  <meta property="og:title" content="Best Plumber in London | Expert Plumbing Services">
  <meta property="og:description" content="Expert plumbing services in London — available 24/7.">
  <meta property="og:image" content="https://example.com/plumber.jpg">
  <link rel="canonical" href="https://example.com/plumber-london">
  <script type="application/ld+json">
    {"@context":"https://schema.org","@type":"LocalBusiness","name":"Expert Plumbers"}
  </script>
</head>
<body>
  <h1>Expert Plumbing Services in London</h1>
  <p>We provide the best plumbing services in London. Our team of expert plumbers is available 24/7 to handle any emergency.</p>
  <h2>Our Services</h2>
  <p>We offer boiler repairs, bathroom installations, and emergency plumbing. Contact us today.</p>
  <h2>Why Choose Us</h2>
  <p>With over 10 years of experience, our London plumbers are fully qualified and insured.</p>
  <img src="plumber.jpg" alt="Plumber fixing a pipe in London" width="800" height="600" loading="lazy">
  <a href="/contact">Contact us</a>
  <a href="/services">Our services</a>
</body>
</html>
"""

SAMPLE_HTML_BAD = """
<html>
<head></head>
<body>
  <p>Short content.</p>
  <img src="pic.jpg">
</body>
</html>
"""


def test_analyze_meta_tags_good():
    result = analyze_meta_tags.invoke({"html": SAMPLE_HTML_GOOD, "url": "https://example.com/plumber-london"})
    assert result["title"] == "Best Plumber in London | Expert Plumbing Services"
    assert result["og_image"] == "https://example.com/plumber.jpg"
    assert result["canonical"] == "https://example.com/plumber-london"
    assert not any("MISSING" in i for i in result["issues"])


def test_analyze_meta_tags_bad():
    result = analyze_meta_tags.invoke({"html": SAMPLE_HTML_BAD})
    assert "MISSING_TITLE" in " ".join(result["issues"])
    assert "MISSING_META_DESC" in " ".join(result["issues"])


def test_analyze_headings_good():
    result = analyze_headings.invoke({"html": SAMPLE_HTML_GOOD})
    assert result["h1_count"] == 1
    assert result["h2_count"] == 2
    assert not result["issues"]


def test_analyze_headings_bad():
    result = analyze_headings.invoke({"html": SAMPLE_HTML_BAD})
    assert any("MISSING_H1" in i for i in result["issues"])


def test_analyze_content_quality():
    result = analyze_content_quality.invoke({"html": SAMPLE_HTML_GOOD, "focus_keyword": "plumber"})
    assert result["word_count"] > 20
    assert result["internal_links"] > 0
    # Keyword density check should run without errors
    assert result["keyword_density"] >= 0


def test_analyze_images_good():
    result = analyze_images.invoke({"html": SAMPLE_HTML_GOOD})
    assert result["images_without_alt"] == 0
    assert not result["issues"]


def test_analyze_images_bad():
    result = analyze_images.invoke({"html": SAMPLE_HTML_BAD})
    assert result["images_without_alt"] == 1
    assert any("IMAGES_MISSING_ALT" in i for i in result["issues"])


def test_check_schema_markup_good():
    result = check_schema_markup.invoke({"html": SAMPLE_HTML_GOOD})
    assert result["schemas_found"] == 1
    assert "LocalBusiness" in result["schema_types"]
    assert not result["issues"]


def test_check_schema_markup_bad():
    result = check_schema_markup.invoke({"html": SAMPLE_HTML_BAD})
    assert result["schemas_found"] == 0
    assert any("MISSING_SCHEMA" in i for i in result["issues"])


def test_check_canonical():
    result = check_canonical.invoke({"html": SAMPLE_HTML_GOOD, "page_url": "https://example.com/plumber-london"})
    assert result["canonical_url"] == "https://example.com/plumber-london"
    assert result["is_self_referencing"]
    assert not result["issues"]
