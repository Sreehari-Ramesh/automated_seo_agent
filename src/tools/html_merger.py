"""Safer DOM-aware HTML section merger for SEO content updates.

Upgrades over the basic version:
- Weighted semantic scoring (heading > body)
- Protected-section detection (nav/footer/forms/CTAs)
- Safer rewrite thresholding
- Better plain HTML parsing
- More conservative fallback behavior
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal

import structlog
from bs4 import BeautifulSoup

logger = structlog.get_logger(__name__)

PageBuilder = Literal["gutenberg", "elementor", "plain"]

# ---------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------

_REWRITE_MIN_SCORE = 0.22
_REWRITE_STRONG_SCORE = 0.34
_NEW_SECTION_MIN_SCORE = 0.08

_HEADING_WEIGHT = 0.55
_BODY_WEIGHT = 0.30
_TAG_WEIGHT = 0.10
_POSITION_WEIGHT = 0.05

_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for", "of",
    "with", "by", "from", "as", "is", "was", "are", "were", "be", "been",
    "being", "have", "has", "had", "do", "does", "did", "will", "would",
    "could", "should", "may", "might", "can", "this", "that", "these", "those",
    "it", "its", "we", "our", "you", "your", "i", "their", "they", "he", "she",
    "his", "her", "not", "more", "also", "which", "who", "what", "when",
    "where", "how", "all",
})

_PROTECTED_WORDS = frozenset({
    "contact", "footer", "copyright", "newsletter", "subscribe", "menu",
    "navigation", "login", "register", "sign", "form", "search", "privacy",
    "terms", "cookie", "whatsapp", "demo", "book", "quote",
})

_CTA_WORDS = frozenset({
    "contact", "demo", "book", "quote", "call", "start", "trial", "signup",
    "register", "talk", "sales", "touch",
})

# ---------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------

@dataclass
class HtmlSection:
    index: int
    heading: str
    heading_words: frozenset[str]
    body_words: frozenset[str]
    start: int
    end: int
    builder: PageBuilder
    raw_html: str
    block_name: str = ""
    tag_name: str = ""
    classes: tuple[str, ...] = ()
    is_protected: bool = False


# ---------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------

def _words(text: str) -> frozenset[str]:
    return frozenset(
        w for w in re.findall(r"[a-zA-Z]{3,}", text.lower())
        if w not in _STOPWORDS
    )


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def _safe_soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html or "", "lxml")


def _html_text(html: str) -> str:
    return _safe_soup(html).get_text(" ", strip=True)


def _html_to_words(html: str) -> frozenset[str]:
    return _words(_html_text(html))


def _extract_heading_text(html: str) -> str:
    soup = _safe_soup(html)
    h = soup.find(["h1", "h2", "h3", "h4"])
    return h.get_text(" ", strip=True) if h else ""


def _extract_heading_words(html: str) -> frozenset[str]:
    return _words(_extract_heading_text(html))


def _extract_tag_name(html: str) -> str:
    soup = _safe_soup(html)
    first = soup.find(True)
    return first.name.lower() if first and first.name else ""


def _extract_classes(html: str) -> tuple[str, ...]:
    soup = _safe_soup(html)
    first = soup.find(True)
    if not first:
        return ()
    classes = first.get("class", [])
    return tuple(str(c).lower() for c in classes if isinstance(c, str))


def _is_protected_section(html: str, heading: str, classes: tuple[str, ...]) -> bool:
    text_words = _words(_html_text(html))
    heading_words = _words(heading)
    class_words = frozenset(re.findall(r"[a-zA-Z]{3,}", " ".join(classes).lower()))
    combined = text_words | heading_words | class_words

    if combined & _PROTECTED_WORDS:
        return True

    soup = _safe_soup(html)
    if soup.find(["form", "nav", "footer"]):
        return True

    links = soup.find_all("a")
    buttons = soup.find_all(["button"])
    if len(links) + len(buttons) >= 4 and (combined & _CTA_WORDS):
        return True

    return False


# ---------------------------------------------------------------------
# Raw block finder
# ---------------------------------------------------------------------

def _find_tag_block(
    html: str,
    tag: str,
    search_from: int = 0,
    attrs_pattern: str | None = None,
) -> tuple[int, int] | None:
    open_re = re.compile(rf"<{tag}(?:\s[^>]*)?>", re.IGNORECASE | re.DOTALL)
    close_re = re.compile(rf"</{tag}>", re.IGNORECASE)

    pos = search_from
    while True:
        m_open = open_re.search(html, pos)
        if not m_open:
            return None
        if attrs_pattern and not re.search(attrs_pattern, m_open.group(0), re.IGNORECASE):
            pos = m_open.end()
            continue

        depth = 1
        cursor = m_open.end()
        while cursor < len(html) and depth > 0:
            nxt_open = open_re.search(html, cursor)
            nxt_close = close_re.search(html, cursor)
            if not nxt_close:
                break
            if nxt_open and nxt_open.start() < nxt_close.start():
                depth += 1
                cursor = nxt_open.end()
            else:
                depth -= 1
                cursor = nxt_close.end()

        if depth == 0:
            return m_open.start(), cursor

        pos = m_open.end()


def _make_section(
    idx: int,
    html: str,
    start: int,
    end: int,
    builder: PageBuilder,
    block_name: str = "",
) -> HtmlSection:
    heading = _extract_heading_text(html)
    heading_words = _extract_heading_words(html)
    body_words = _html_to_words(html)
    tag_name = _extract_tag_name(html)
    classes = _extract_classes(html)
    protected = _is_protected_section(html, heading, classes)
    return HtmlSection(
        index=idx,
        heading=heading,
        heading_words=heading_words,
        body_words=body_words,
        start=start,
        end=end,
        builder=builder,
        raw_html=html,
        block_name=block_name,
        tag_name=tag_name,
        classes=classes,
        is_protected=protected,
    )


# ---------------------------------------------------------------------
# Builder detection
# ---------------------------------------------------------------------

def detect_builder(html: str) -> PageBuilder:
    if "<!-- wp:" in html:
        return "gutenberg"
    if re.search(r'class="[^"]*elementor|data-elementor', html or ""):
        return "elementor"
    return "plain"


# ---------------------------------------------------------------------
# Gutenberg parsing
# ---------------------------------------------------------------------

_GB_OPEN_RE = re.compile(r"<!-- wp:(?P<name>[\w/:-]+)(?:\s[^>]*)? -->")
_GB_SELF_CLOSE_RE = re.compile(r"<!-- wp:(?P<name>[\w/:-]+)(?:\s[^>]*)? /-->")

def _parse_gutenberg(html: str) -> list[HtmlSection]:
    sections: list[HtmlSection] = []
    idx = 0
    pos = 0

    while pos < len(html):
        sc = _GB_SELF_CLOSE_RE.search(html, pos)
        mo = _GB_OPEN_RE.search(html, pos)

        if not mo and not sc:
            break

        if sc and (not mo or sc.start() < mo.start()):
            block_html = html[sc.start():sc.end()]
            sections.append(_make_section(
                idx, block_html, sc.start(), sc.end(), "gutenberg", sc.group("name")
            ))
            idx += 1
            pos = sc.end()
            continue

        block_name = mo.group("name")
        close_pat = re.compile(rf"<!-- /wp:{re.escape(block_name)} -->")
        open_pat = re.compile(rf"<!-- wp:{re.escape(block_name)}(?:\s[^>]*)? -->")
        depth = 1
        cursor = mo.end()

        while cursor < len(html) and depth > 0:
            nxt_open = open_pat.search(html, cursor)
            nxt_close = close_pat.search(html, cursor)
            if not nxt_close:
                break
            if nxt_open and nxt_open.start() < nxt_close.start():
                depth += 1
                cursor = nxt_open.end()
            else:
                depth -= 1
                cursor = nxt_close.end()

        if depth != 0:
            pos = mo.end()
            continue

        block_html = html[mo.start():cursor]
        sections.append(_make_section(
            idx, block_html, mo.start(), cursor, "gutenberg", block_name
        ))
        idx += 1
        pos = cursor

    return sections if sections else _parse_plain(html)


# ---------------------------------------------------------------------
# Elementor parsing
# ---------------------------------------------------------------------

def _parse_elementor(html: str) -> list[HtmlSection]:
    sections: list[HtmlSection] = []
    idx = 0
    pos = 0

    while True:
        result = _find_tag_block(
            html, "section", pos,
            attrs_pattern=r'class="[^"]*elementor-(?:top-)?section',
        )
        if not result:
            result = _find_tag_block(
                html, "div", pos,
                attrs_pattern=r'data-element_type="container"',
            )
        if not result:
            break

        start, end = result
        block_html = html[start:end]
        sections.append(_make_section(idx, block_html, start, end, "elementor"))
        idx += 1
        pos = end

    return sections if sections else _parse_plain(html)


# ---------------------------------------------------------------------
# Plain HTML parsing
# ---------------------------------------------------------------------

def _parse_plain(html: str) -> list[HtmlSection]:
    sections: list[HtmlSection] = []
    idx = 0
    pos = 0

    while True:
        result = _find_tag_block(html, "section", pos)
        if not result:
            break
        start, end = result
        block_html = html[start:end]
        sections.append(_make_section(idx, block_html, start, end, "plain"))
        idx += 1
        pos = end

    if sections:
        return sections

    boundaries = [m.start() for m in re.finditer(r"<h[1-4][\s>]", html, re.IGNORECASE)]
    if len(boundaries) >= 2:
        edges = boundaries + [len(html)]
        for i in range(len(edges) - 1):
            start, end = edges[i], edges[i + 1]
            block_html = html[start:end]
            sections.append(_make_section(idx, block_html, start, end, "plain"))
            idx += 1
        return sections

    div_sections: list[HtmlSection] = []
    idx = 0
    pos = 0
    while True:
        result = _find_tag_block(html, "div", pos)
        if not result:
            break
        start, end = result
        block_html = html[start:end]
        text = _html_text(block_html)
        if len(text) >= 80:
            div_sections.append(_make_section(idx, block_html, start, end, "plain"))
            idx += 1
        pos = end

    if div_sections:
        return div_sections

    return [_make_section(0, html, 0, len(html), "plain")]


# ---------------------------------------------------------------------
# Public parser
# ---------------------------------------------------------------------

def parse_sections(html: str) -> tuple[PageBuilder, list[HtmlSection]]:
    builder = detect_builder(html)
    if builder == "gutenberg":
        return builder, _parse_gutenberg(html)
    if builder == "elementor":
        return builder, _parse_elementor(html)
    return builder, _parse_plain(html)


# ---------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------

def _position_bias(section_idx: int, total: int) -> float:
    if total <= 1:
        return 0.5
    return 1.0 - (section_idx / max(total - 1, 1))


def _new_html_signature(new_html: str) -> tuple[frozenset[str], frozenset[str], str]:
    return (
        _extract_heading_words(new_html),
        _html_to_words(new_html),
        _extract_tag_name(new_html),
    )


def _score_section(
    section: HtmlSection,
    new_heading_words: frozenset[str],
    new_body_words: frozenset[str],
    new_tag: str,
    total_sections: int,
) -> float:
    heading_score = _jaccard(section.heading_words, new_heading_words)
    body_score = _jaccard(section.body_words, new_body_words)

    tag_score = 1.0 if new_tag and section.tag_name and new_tag == section.tag_name else 0.0
    position_score = _position_bias(section.index, total_sections)

    score = (
        _HEADING_WEIGHT * heading_score +
        _BODY_WEIGHT * body_score +
        _TAG_WEIGHT * tag_score +
        _POSITION_WEIGHT * position_score
    )

    if section.is_protected:
        score *= 0.45

    return score


def _best_match(sections: list[HtmlSection], new_html: str) -> tuple[HtmlSection, float]:
    new_heading_words, new_body_words, new_tag = _new_html_signature(new_html)

    best_section = sections[0]
    best_score = -1.0

    for section in sections:
        score = _score_section(
            section,
            new_heading_words,
            new_body_words,
            new_tag,
            len(sections),
        )
        if score > best_score:
            best_section = section
            best_score = score

    return best_section, best_score


# ---------------------------------------------------------------------
# Merge helpers
# ---------------------------------------------------------------------

def _replace_range(existing_html: str, start: int, end: int, replacement: str) -> str:
    return existing_html[:start] + replacement + existing_html[end:]


def _insert_after(existing_html: str, offset: int, insertion: str) -> str:
    return existing_html[:offset] + "\n" + insertion + existing_html[offset:]


# ---------------------------------------------------------------------
# Public merge API
# ---------------------------------------------------------------------

def merge_content_rewrite(existing_html: str, new_html: str) -> str:
    """Replace the most relevant section, or fall back safely.

    Behavior:
    - Strong match: replace best section only
    - Weak/no match: append at end if new content looks sectional
    - Empty existing: return new_html
    """
    if not existing_html:
        return new_html

    builder, sections = parse_sections(existing_html)
    if not sections:
        return new_html

    best_section, score = _best_match(sections, new_html)

    logger.info(
        "html_merger.rewrite",
        builder=builder,
        section_heading=best_section.heading or best_section.block_name,
        score=round(score, 3),
        protected=best_section.is_protected,
    )

    if score >= _REWRITE_STRONG_SCORE:
        return _replace_range(existing_html, best_section.start, best_section.end, new_html)

    if score >= _REWRITE_MIN_SCORE and not best_section.is_protected:
        return _replace_range(existing_html, best_section.start, best_section.end, new_html)

    # Safer fallback than full-page replace:
    # if the new content appears to be a section, append it — but only if it
    # isn't already present (same duplicate check as merge_new_section).
    new_heading = _extract_heading_text(new_html)
    if new_heading or re.search(r"<section|<div|<!-- wp:", new_html, re.IGNORECASE):
        new_hw = _extract_heading_words(new_html)
        new_bw = _html_to_words(new_html)
        for section in sections:
            if (new_heading and section.heading and
                    new_heading.lower().strip() == section.heading.lower().strip()):
                logger.info("html_merger.rewrite_fallback_skipped", reason="duplicate_heading")
                return existing_html
            h_sim = _jaccard(section.heading_words, new_hw)
            b_sim = _jaccard(section.body_words, new_bw)
            if h_sim >= 0.6 or (h_sim >= 0.3 and b_sim >= 0.4):
                logger.info("html_merger.rewrite_fallback_skipped", reason="semantic_duplicate")
                return existing_html
        logger.info("html_merger.rewrite_fallback_append", score=round(score, 3))
        return _insert_after(existing_html, len(existing_html), new_html)

    logger.info("html_merger.rewrite_fallback_full_replace", score=round(score, 3))
    return new_html


def merge_new_section(existing_html: str, new_section_html: str) -> str:
    """Insert new content after the most relevant section.

    If no useful match is found, append to the end.
    Skips insertion entirely if a semantically identical section already exists.
    """
    if not existing_html:
        return new_section_html

    builder, sections = parse_sections(existing_html)
    if not sections:
        return existing_html + "\n" + new_section_html

    # ── Deduplication: don't insert if this section already exists ────────────
    new_heading = _extract_heading_text(new_section_html)
    new_heading_words = _extract_heading_words(new_section_html)
    new_body_words = _html_to_words(new_section_html)

    for section in sections:
        # Exact heading match (case-insensitive)
        if (new_heading and section.heading and
                new_heading.lower().strip() == section.heading.lower().strip()):
            logger.info(
                "html_merger.new_section_skipped",
                reason="duplicate_heading",
                heading=new_heading,
            )
            return existing_html

        # High semantic overlap: heading strongly overlaps, or heading + body both overlap
        heading_sim = _jaccard(section.heading_words, new_heading_words)
        body_sim = _jaccard(section.body_words, new_body_words)
        if heading_sim >= 0.6 or (heading_sim >= 0.3 and body_sim >= 0.4):
            logger.info(
                "html_merger.new_section_skipped",
                reason="semantic_duplicate",
                heading_sim=round(heading_sim, 3),
                body_sim=round(body_sim, 3),
            )
            return existing_html

    best_section, score = _best_match(sections, new_section_html)

    logger.info(
        "html_merger.new_section",
        builder=builder,
        anchor_heading=best_section.heading or best_section.block_name,
        score=round(score, 3),
        protected=best_section.is_protected,
        insert_after_offset=best_section.end,
    )

    if score < _NEW_SECTION_MIN_SCORE:
        return existing_html.rstrip() + "\n" + new_section_html

    return _insert_after(existing_html, best_section.end, new_section_html)


def merge_schema(existing_html: str, schema_script: str) -> str:
    """Append JSON-LD, deduplicating by @type."""
    schema_type = ""
    m = re.search(r"<script[^>]*>(.*?)</script>", schema_script, re.DOTALL)
    if m:
        try:
            schema_type = json.loads(m.group(1).strip()).get("@type", "")
        except Exception:
            pass

    if existing_html and schema_type:
        def _drop_same_type(match: re.Match) -> str:
            try:
                if json.loads(match.group(1).strip()).get("@type") == schema_type:
                    return ""
            except Exception:
                pass
            return match.group(0)

        existing_html = re.sub(
            r'<script\s+type="application/ld\+json">(.*?)</script>',
            _drop_same_type,
            existing_html,
            flags=re.DOTALL,
        )

    return (existing_html or "").rstrip() + "\n" + schema_script