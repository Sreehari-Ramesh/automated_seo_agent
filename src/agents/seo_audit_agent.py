"""SEO Audit Agent - analyzes all crawled pages and produces a ranked issue list."""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from langchain_anthropic import ChatAnthropic  # noqa: F401  # production provider
from langchain_openai import ChatOpenAI  # noqa: F401  # optional local provider
from langchain_core.messages import HumanMessage, SystemMessage

from src.agents.state import CrawledPage, SEOAgentState, SEOIssue
from src.config import get_settings
from src.observability.metrics import accumulate_token_usage
from src.tools.crawler_tools import check_page_speed
from src.tools.seo_tools import (
    analyze_content_quality,
    analyze_headings,
    analyze_images,
    analyze_meta_tags,
    check_canonical,
    check_schema_markup,
)

settings = get_settings()
logger = structlog.get_logger(__name__)


# LLM
_llm = ChatAnthropic(
    model=settings.claude_model,
    temperature=0.4,
    max_tokens=512,
    anthropic_api_key=settings.anthropic_api_key,
)
# _llm = ChatOpenAI(
#     model=settings.openai_model,
#     temperature=0.4,
#     max_tokens=settings.openai_max_tokens,
#     api_key=settings.openai_api_key,
# )

_SYSTEM_PROMPT = SystemMessage(content="""You are a world-class SEO Audit Agent.
For each issue found assign a severity (critical|high|medium|low), estimate impact
score (0-100), and suggest a specific actionable fix.
Prioritize issues with the highest ranking impact.""")


_SEVERITY_SCORE = {"critical": 90, "high": 70, "medium": 45, "low": 20}
_SEVERITY_MAP = {
    "MISSING_TITLE": ("critical", "meta"),
    "TITLE_TOO_SHORT": ("high", "meta"),
    "TITLE_TOO_LONG": ("medium", "meta"),
    "MISSING_META_DESC": ("high", "meta"),
    "META_DESC_TOO_SHORT": ("medium", "meta"),
    "META_DESC_TOO_LONG": ("medium", "meta"),
    "MISSING_OG_TITLE": ("medium", "meta"),
    "MISSING_OG_DESC": ("medium", "meta"),
    "MISSING_OG_IMAGE": ("low", "meta"),
    "MISSING_CANONICAL": ("high", "technical"),
    "CANONICAL_MISMATCH": ("high", "technical"),
    "MULTIPLE_CANONICAL": ("critical", "technical"),
    "NOINDEX_SET": ("critical", "technical"),
    "MISSING_H1": ("critical", "structure"),
    "MULTIPLE_H1": ("high", "structure"),
    "MISSING_H2": ("medium", "structure"),
    "HEADING_SKIP": ("low", "structure"),
    "THIN_CONTENT": ("high", "content"),
    "LOW_KEYWORD_DENSITY": ("medium", "content"),
    "KEYWORD_STUFFING": ("high", "content"),
    "LOW_PARAGRAPH_COUNT": ("medium", "content"),
    "LONG_SENTENCES": ("low", "content"),
    "NO_INTERNAL_LINKS": ("high", "structure"),
    "IMAGES_MISSING_ALT": ("high", "technical"),
    "IMAGES_NOT_LAZY": ("low", "performance"),
    "MISSING_SCHEMA": ("medium", "schema"),
    "INVALID_SCHEMA": ("high", "schema"),
    # ── PageSpeed / Core Web Vitals ───────────────────────────────────────────
    "VERY_SLOW_LCP": ("critical", "performance"),
    "SLOW_LCP": ("high", "performance"),
    "VERY_HIGH_CLS": ("critical", "performance"),
    "HIGH_CLS": ("high", "performance"),
    "SLOW_FCP": ("medium", "performance"),
    "SLOW_TTFB": ("medium", "performance"),
    "LOW_PERFORMANCE_SCORE": ("high", "performance"),
    "LOW_ACCESSIBILITY_SCORE": ("medium", "technical"),
    "LOW_PSI_SEO_SCORE": ("high", "meta"),
}


def _compact_list(values: list[Any], limit: int = 3) -> str:
    items = [str(v).strip() for v in values if str(v).strip()]
    if not items:
        return "none"
    head = items[:limit]
    extra = len(items) - len(head)
    return ", ".join(head) + (f" (+{extra} more)" if extra > 0 else "")


def _build_previous_context_note(prev: dict[str, Any]) -> str:
    if not prev:
        return ""

    changes_summary = str(prev.get("changes_summary", "")).strip()
    if changes_summary:
        return (
            f"\nPrevious run context ({prev.get('completed_at', 'unknown date')}):\n"
            f"  - Score was: {prev.get('seo_score', '?')}/100\n"
            f"  - {changes_summary}\n"
            "Use this as context only; prioritize currently unresolved issues.\n"
        )

    return (
        f"\nPrevious run context ({prev.get('completed_at', 'unknown date')}):\n"
        f"  - Score was: {prev.get('seo_score', '?')}/100\n"
        f"  - Changes applied last run: {prev.get('changes_applied', 0)}\n"
        f"  - Change types: {_compact_list(prev.get('applied_change_types', []), 3)}\n"
        f"  - Pages changed: {_compact_list(prev.get('pages_changed', []), 3)}\n"
        f"  - Top prior issues: {_compact_list([i.get('rule_id', '') for i in prev.get('top_issues', [])], 5)}\n"
        "Use this as context only; prioritize currently unresolved issues.\n"
    )


def _parse_issues_from_analysis(
    page: CrawledPage,
    meta: dict,
    headings: dict,
    content: dict,
    images: dict,
    schema: dict,
    canonical: dict,
) -> list[SEOIssue]:
    """Convert raw tool outputs into typed SEOIssue objects."""
    issues: list[SEOIssue] = []
    all_raw = (
        meta.get("issues", [])
        + headings.get("issues", [])
        + content.get("issues", [])
        + images.get("issues", [])
        + schema.get("issues", [])
        + canonical.get("issues", [])
    )

    for raw in all_raw:
        parts = raw.split(":", 1)
        rule_id = parts[0].strip() if parts else raw
        description = parts[1].strip() if len(parts) > 1 else raw
        severity, category = _SEVERITY_MAP.get(rule_id, ("medium", "technical"))
        impact = _SEVERITY_SCORE.get(severity, 45)

        current_value: str | None = None
        if "TITLE" in rule_id:
            current_value = meta.get("title", "")
        elif "META_DESC" in rule_id:
            current_value = meta.get("meta_description", "")
        elif "CANONICAL" in rule_id:
            current_value = canonical.get("canonical_url", "")
        elif "H1" in rule_id or "H2" in rule_id:
            current_value = str(headings.get("h1_texts", []))
        elif "CONTENT" in rule_id or "KEYWORD" in rule_id:
            current_value = f"Word count: {content.get('word_count', 0)}"
        elif "IMAGE" in rule_id:
            current_value = f"{images.get('images_without_alt', 0)} images without alt"

        issues.append(
            SEOIssue(
                rule_id=rule_id,
                category=category,
                severity=severity,
                title=f"{rule_id.replace('_', ' ').title()}",
                description=description,
                current_value=current_value,
                suggested_fix=None,
                impact_score=float(impact),
                page_url=page["url"],
                wp_post_id=page.get("wp_post_id"),
                post_type=page.get("post_type", "page"),
            )
        )

    return issues


def _psi_issues_from_result(page: CrawledPage, psi: dict) -> list[SEOIssue]:
    """Convert a PageSpeed Insights result into typed SEOIssue objects."""
    issues: list[SEOIssue] = []

    def _make(rule_id: str, description: str, current_value: str | None = None) -> SEOIssue:
        severity, category = _SEVERITY_MAP.get(rule_id, ("medium", "performance"))
        return SEOIssue(
            rule_id=rule_id,
            category=category,
            severity=severity,
            title=rule_id.replace("_", " ").title(),
            description=description,
            current_value=current_value,
            suggested_fix=None,
            impact_score=float(_SEVERITY_SCORE.get(severity, 45)),
            page_url=page["url"],
            wp_post_id=page.get("wp_post_id"),
            post_type=page.get("post_type", "page"),
        )

    lcp = psi.get("lcp")       # milliseconds
    cls = psi.get("cls")       # unitless ratio
    fcp = psi.get("fcp")       # milliseconds
    ttfb = psi.get("ttfb")     # milliseconds
    perf = psi.get("performance_score")
    a11y = psi.get("accessibility_score")
    seo_score = psi.get("seo_score")

    if lcp is not None:
        if lcp > 4000:
            issues.append(_make("VERY_SLOW_LCP",
                f"LCP is {lcp/1000:.1f}s — Google requires under 2.5s (critical ranking factor)",
                f"{lcp/1000:.2f}s"))
        elif lcp > 2500:
            issues.append(_make("SLOW_LCP",
                f"LCP is {lcp/1000:.1f}s — Google recommends under 2.5s",
                f"{lcp/1000:.2f}s"))

    if cls is not None:
        if cls > 0.25:
            issues.append(_make("VERY_HIGH_CLS",
                f"CLS is {cls:.3f} — severe layout shift hurts UX and rankings (target < 0.1)",
                f"{cls:.3f}"))
        elif cls > 0.1:
            issues.append(_make("HIGH_CLS",
                f"CLS is {cls:.3f} — layout shift detected (Google recommends < 0.1)",
                f"{cls:.3f}"))

    if fcp is not None and fcp > 1800:
        issues.append(_make("SLOW_FCP",
            f"FCP is {fcp/1000:.1f}s — first content visible too slowly (target < 1.8s)",
            f"{fcp/1000:.2f}s"))

    if ttfb is not None and ttfb > 600:
        issues.append(_make("SLOW_TTFB",
            f"TTFB is {ttfb}ms — server response too slow (target < 600ms)",
            f"{ttfb}ms"))

    if perf is not None and perf < 50:
        issues.append(_make("LOW_PERFORMANCE_SCORE",
            f"Lighthouse performance score is {perf}/100 — page is too slow for good rankings",
            str(perf)))

    if a11y is not None and a11y < 70:
        issues.append(_make("LOW_ACCESSIBILITY_SCORE",
            f"Lighthouse accessibility score is {a11y}/100 — affects user experience and SEO",
            str(a11y)))

    if seo_score is not None and seo_score < 80:
        issues.append(_make("LOW_PSI_SEO_SCORE",
            f"Lighthouse SEO score is {seo_score}/100 — technical SEO gaps detected by Google",
            str(seo_score)))

    return issues


def _compute_page_score(issues: list[SEOIssue]) -> float:
    """Compute a 0-100 SEO score for a page based on its issues."""
    if not issues:
        return 100.0
    penalty = {"critical": 20, "high": 12, "medium": 6, "low": 2}
    total_penalty = sum(penalty.get(issue["severity"], 4) for issue in issues)
    return max(15.0, round(100.0 - min(total_penalty, 85.0), 1))


async def _audit_page(page: CrawledPage) -> tuple[dict[str, Any], list[SEOIssue]]:
    """Run all six SEO tools directly (no HTML sent to LLM)."""
    html = page["html"]

    meta = analyze_meta_tags.invoke({"html": html, "url": page["url"]})
    headings = analyze_headings.invoke({"html": html})
    content = analyze_content_quality.invoke({"html": html, "focus_keyword": ""})
    images = analyze_images.invoke({"html": html})
    schema = check_schema_markup.invoke({"html": html})
    canonical = check_canonical.invoke({"html": html, "page_url": page["url"]})

    issues = _parse_issues_from_analysis(page, meta, headings, content, images, schema, canonical)
    page_score = _compute_page_score(issues)

    audit_detail = {
        "url": page["url"],
        "wp_post_id": page.get("wp_post_id"),
        "post_type": page.get("post_type", "page"),
        "page_score": page_score,
        "issue_count": len(issues),
        "meta": meta,
        "headings": headings,
        "content": content,
        "images": images,
        "schema": schema,
        "canonical": canonical,
    }
    return audit_detail, issues


async def seo_audit_node(state: SEOAgentState) -> dict:
    """LangGraph node: runs SEO audit on all crawled pages."""

    logger.info("seo_audit_agent.start", pages=len(state.get("crawled_pages", [])))
    errors: list[str] = list(state.get("errors", []))
    all_issues: list[SEOIssue] = []
    audit_results: list[dict[str, Any]] = []

    pages = state.get("crawled_pages", [])
    if not pages:
        errors.append("SEO Audit: no crawled pages to audit")
        return {"issues": [], "audit_results": [], "overall_seo_score": 0.0, "errors": errors}

    for page in pages:
        if not page.get("html"):
            continue
        try:
            audit_detail, page_issues = await _audit_page(page)
            audit_results.append(audit_detail)
            all_issues.extend(page_issues)
        except Exception as exc:
            errors.append(f"Audit error for {page['url']}: {exc}")

    # ── PSI / Core Web Vitals for the 50 lowest-scoring pages ───────────────
    _PSI_MAX_PAGES = 50
    _PSI_CONCURRENCY = 2

    url_to_page: dict[str, CrawledPage] = {
        p["url"]: p for p in pages if p.get("html")
    }
    # Prioritise worst pages (most likely to have real performance issues)
    psi_candidates = sorted(audit_results, key=lambda r: r["page_score"])[:_PSI_MAX_PAGES]

    psi_sem = asyncio.Semaphore(_PSI_CONCURRENCY)

    async def _run_psi(audit_detail: dict) -> list[SEOIssue]:
        page = url_to_page.get(audit_detail["url"])
        if not page:
            return []
        async with psi_sem:
            try:
                psi = await check_page_speed.ainvoke({"url": audit_detail["url"]})
                await asyncio.sleep(0.3)
            except Exception:
                return []
        if psi.get("error") or psi.get("performance_score") is None:
            return []
        audit_detail["psi"] = psi
        return _psi_issues_from_result(page, psi)

    psi_batches = await asyncio.gather(
        *[_run_psi(r) for r in psi_candidates], return_exceptions=True
    )
    for batch in psi_batches:
        if isinstance(batch, list):
            all_issues.extend(batch)

    all_issues.sort(key=lambda x: x["impact_score"], reverse=True)

    overall_score = (
        round(sum(r["page_score"] for r in audit_results) / len(audit_results), 1)
        if audit_results
        else 0.0
    )

    issue_summary: list[str] = []
    for issue in all_issues[:12]:
        desc = str(issue.get("description", "")).strip()
        if len(desc) > 120:
            desc = desc[:117] + "..."
        issue_summary.append(f"- [{issue['severity'].upper()}] {issue['title']}: {desc}")

    prev_context_note = _build_previous_context_note(state.get("previous_run_context", {}))

    llm_response = await _llm.ainvoke(
        [
            _SYSTEM_PROMPT,
            HumanMessage(
                content=(
                    f"Site: {state['target_url']}\n"
                    f"Pages audited: {len(audit_results)}\n"
                    f"Overall SEO score: {overall_score}/100\n"
                    f"{prev_context_note}"
                    f"Top issues found:\n" + "\n".join(issue_summary) + "\n\n"
                    "Please provide:\n"
                    "1. A 2-sentence executive summary of the site's SEO health\n"
                    "2. Top 3 highest-priority areas to fix\n"
                    "3. Estimated traffic impact if top issues are resolved"
                )
            ),
        ]
    )
    accumulate_token_usage(llm_response)
    ai_summary = llm_response.content if hasattr(llm_response, "content") else str(llm_response)
    ai_summary = ai_summary[:900].strip()

    logger.info(
        "seo_audit_agent.complete",
        issues_found=len(all_issues),
        overall_score=overall_score,
    )

    print(
        "################################\n"
        f"SEO Audit complete. audited {len(audit_results)} pages with {len(all_issues)} total issues. "
        f"Overall SEO score: {overall_score}/100.\n"
        f"AI audit summary:\n{ai_summary}\n"
        "################################"
    )

    return {
        "audit_results": audit_results,
        "issues": all_issues,
        "overall_seo_score": overall_score,
        "errors": errors,
        "messages": [
            HumanMessage(
                content=f"SEO Audit complete. {len(all_issues)} issues across {len(audit_results)} pages. "
                f"Overall score: {overall_score}/100.\n\nAI Analysis:\n{ai_summary}"
            )
        ],
    }
