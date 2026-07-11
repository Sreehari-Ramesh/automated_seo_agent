"""Content Generator Agent — creates optimized meta tags, improved content, and new pages."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any  # noqa: F401

import structlog
from langchain_anthropic import ChatAnthropic  # noqa: F401  # production provider
from langchain_core.messages import HumanMessage, SystemMessage
from src.agents.state import (
    ContentSuggestion,
    ExecutionTask,
    SEOAgentState,
    SEOIssue,
)
from src.config import get_settings
from src.observability.metrics import accumulate_token_usage

settings = get_settings()
logger = structlog.get_logger(__name__)

# Single combined LLM instance used for all per-page content generation.
# max_tokens covers the largest possible combined output:
#   meta (~300) + schema (~600) + rewrite (~900) = 1800.
# When only one task applies, the model returns far fewer tokens — no waste.
_llm_combined = ChatAnthropic(
    model=settings.claude_model,
    temperature=0.3,
    max_tokens=1800,
    anthropic_api_key=settings.anthropic_api_key,
)

# Lightweight repair LLM — only used to fix malformed JSON responses.
_llm_repair = ChatAnthropic(
    model=settings.claude_model,
    temperature=0.0,
    max_tokens=600,
    anthropic_api_key=settings.anthropic_api_key,
)

# Kept for future new-page generation (currently disabled).
_llm_page = ChatAnthropic(
    model=settings.claude_model,
    temperature=0.4,
    max_tokens=2048,
    anthropic_api_key=settings.anthropic_api_key,
)

# # OpenAI equivalents (swap in if needed):
# _llm_combined = ChatOpenAI(model=settings.openai_model, temperature=0.3, max_tokens=1800, api_key=settings.openai_api_key)
# _llm_repair   = ChatOpenAI(model=settings.openai_model, temperature=0.0, max_tokens=600,  api_key=settings.openai_api_key)
# _llm_page     = ChatOpenAI(model=settings.openai_model, temperature=0.4, max_tokens=2048, api_key=settings.openai_api_key)

# System prompt with cache_control so Anthropic caches it after the first call.
# Cached tokens are billed at 10% of normal input cost — every subsequent call
# in the same session re-uses the cache instead of re-transmitting ~200 tokens.
_SYSTEM_PROMPT = SystemMessage(content=[{
    "type": "text",
    "text": (
        "You are an expert SEO Content Strategist and Copywriter.\n\n"
        "Your role is to generate optimized SEO content improvements:\n"
        "1. Write compelling meta titles (50-60 chars) that include the focus keyword naturally\n"
        "2. Write engaging meta descriptions (150-160 chars) with a clear CTA\n"
        "3. Suggest content additions or rewrites for thin/poorly-optimized pages\n"
        "4. Generate complete new landing pages for identified content gaps\n"
        "5. Create JSON-LD schema markup for appropriate page types\n\n"
        "Rules:\n"
        "- Never keyword stuff — write for humans first, search engines second\n"
        "- Each meta title must be unique across the site\n"
        "- Meta descriptions must be compelling enough to earn clicks\n"
        "- New page content must be at least 600 words and genuinely useful\n"
        "- Schema markup must follow Schema.org specifications exactly\n"
        "- Always explain the SEO rationale for each suggestion"
    ),
    "cache_control": {"type": "ephemeral"},
}])

# Custom AI meta keys — must match seo-agent-meta.php (ai_* fields)
_AI_TITLE_KEY   = "ai_meta_title"
_AI_DESC_KEY    = "ai_meta_description"
_AI_KW_KEY      = "ai_focus_keyword"

_REWRITE_RULES = {"MISSING_H1", "MULTIPLE_H1", "KEYWORD_STUFFING", "LONG_SENTENCES", "HEADING_SKIP"}


def _strip_code_fences(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z0-9_-]*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned)
    return cleaned.strip()


def _extract_json_blob(text: str) -> str:
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return text


async def _parse_json_with_repair(
    raw: str,
    *,
    url: str,
    parse_kind: str,
    repair_llm: Any,
) -> dict[str, Any]:
    """Parse model JSON robustly; attempt one repair pass if malformed."""
    cleaned = _strip_code_fences(raw)
    candidates = [cleaned, _extract_json_blob(cleaned)]

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue

    # One repair pass: ask LLM to output strict JSON only.
    repair_prompt = (
        "Fix the following malformed JSON. Return ONLY valid JSON with the same keys and values. "
        "Do not add explanations.\n\n"
        f"MALFORMED JSON:\n{cleaned}"
    )
    repair_resp = await repair_llm.ainvoke([HumanMessage(content=repair_prompt)])
    accumulate_token_usage(repair_resp)
    repaired = _strip_code_fences(repair_resp.content if hasattr(repair_resp, "content") else str(repair_resp))
    repaired = _extract_json_blob(repaired)
    try:
        return json.loads(repaired)
    except json.JSONDecodeError as exc:
        logger.warning(f"content_generator.{parse_kind}_parse_error", url=url, error=str(exc))
        return {}


def _format_competitive_context(competitive_insights: list[dict], keyword: str) -> str:
    """Build a compact competitive benchmark string for LLM prompts."""
    for insight in competitive_insights:
        if insight.get("keyword", "").lower() == keyword.lower():
            s = insight.get("summary", {})
            top = (insight.get("competitors") or [{}])[0]
            lines = [
                f"Competitor benchmark for '{keyword}':",
                f"  - Avg word count of top {len(insight['competitors'])} results: {s.get('avg_word_count', '?')} words",
                f"  - Avg H2 count: {s.get('avg_h2_count', '?')}",
                f"  - Schema types used: {', '.join(s.get('common_schemas', [])) or 'none'}",
                f"  - Top competitor: {top.get('url', '?')} ({top.get('word_count', '?')} words)",
            ]
            if top.get("h2s"):
                lines.append(f"  - Top competitor H2s: {' | '.join(top['h2s'][:4])}")
            return "\n".join(lines)
    return ""


async def _generate_page_improvements(
    page: dict,
    issues: list[SEOIssue],
    primary_keyword: str,
    competitive_insights: list[dict] | None = None,
    *,
    needs_meta: bool = False,
    needs_schema: bool = False,
    needs_rewrite: bool = False,
) -> list[ContentSuggestion]:
    """Single consolidated LLM call for all per-page improvements.

    Instead of making 3 separate API calls (meta, schema, rewrite), this sends
    one request per page that handles every applicable task in a single prompt.
    Page context (URL, title, keyword, content preview) is transmitted once,
    and the system prompt is cached by Anthropic after the first call.

    Only tasks flagged True are included in the prompt, so a page that only
    needs meta tags still gets a small, focused prompt — no wasted tokens.
    """
    if not (needs_meta or needs_schema or needs_rewrite):
        return []

    url = page.get("url", "")
    title = page.get("title", "")
    text_snippet = page.get("text", "")[:300]

    task_sections: list[str] = []
    response_shape_parts: list[str] = []

    # ── Task 1: Meta tags ────────────────────────────────────────────────────
    current_title = title
    current_meta = ""
    meta_issues: list[SEOIssue] = []

    if needs_meta:
        meta_issues = [i for i in issues if i["category"] == "meta"]
        for issue in meta_issues:
            if "META_DESC" in issue.get("rule_id", ""):
                current_meta = issue.get("current_value", "") or ""

        task_sections.append(
            f"TASK 1 — META TAGS\n"
            f"Current Title: {current_title}\n"
            f"Current Meta Description: {current_meta or 'MISSING'}\n"
            f"Issues to fix: {[i['title'] for i in meta_issues]}"
        )
        response_shape_parts.append(
            f'"meta": {{"meta_title": "50-60 chars including \'{primary_keyword}\'", '
            f'"meta_description": "150-160 chars with CTA", '
            f'"focus_keyword": "{primary_keyword}", '
            f'"rationale": "brief explanation"}}'
        )

    # ── Task 2: JSON-LD Schema ───────────────────────────────────────────────
    if needs_schema:
        task_sections.append(
            "TASK 2 — JSON-LD SCHEMA\n"
            "Determine the most appropriate schema type (Article, WebPage, Service, "
            "LocalBusiness, FAQPage, etc.) and generate complete, valid JSON-LD."
        )
        response_shape_parts.append(
            '"schema": {"schema_type": "...", "json_ld": {complete JSON-LD object}, "rationale": "..."}'
        )

    # ── Task 3: Content rewrite ──────────────────────────────────────────────
    rewrite_issues = [i for i in issues if i["rule_id"] in _REWRITE_RULES] if needs_rewrite else []
    if needs_rewrite and rewrite_issues:
        issue_descriptions = "\n".join(f"- {i['rule_id']}: {i['description']}" for i in rewrite_issues)
        comp_context = _format_competitive_context(competitive_insights or [], primary_keyword)
        comp_note = f"\n{comp_context}" if comp_context else ""

        task_sections.append(
            f"TASK 3 — CONTENT REWRITE\n"
            f"Issues requiring a section rewrite:\n{issue_descriptions}{comp_note}\n"
            f"Generate a replacement HTML section: single <h1>/<h2> heading, "
            f"natural use of \"{primary_keyword}\", short readable sentences, complete self-contained HTML."
        )
        response_shape_parts.append(
            '"rewrite": {"target_section": "brief description of which section this replaces", '
            '"html": "<h2>heading</h2><p>para 1</p><p>para 2</p>", "rationale": "what was fixed and why"}'
        )
    else:
        needs_rewrite = False  # No qualifying rewrite issues found

    if not task_sections:
        return []

    task_block = "\n\n".join(task_sections)
    keys_block = ",\n  ".join(response_shape_parts)

    prompt = (
        f"Analyze this WordPress page and complete ONLY the tasks listed below.\n\n"
        f"Page URL: {url}\n"
        f"Page Title: {title}\n"
        f"Primary Keyword: {primary_keyword}\n"
        f"Content Preview: {text_snippet}\n\n"
        f"{task_block}\n\n"
        f"Respond with a single JSON object. Include only the keys for the tasks listed above "
        f"(valid JSON, escape all inner quotes):\n"
        f"{{\n  {keys_block}\n}}"
    )

    response = await _llm_combined.ainvoke([_SYSTEM_PROMPT, HumanMessage(content=prompt)])
    accumulate_token_usage(response)

    raw = response.content if hasattr(response, "content") else str(response)
    data = await _parse_json_with_repair(raw, url=url, parse_kind="combined", repair_llm=_llm_repair)

    if not data:
        return []

    suggestions: list[ContentSuggestion] = []

    # ── Parse meta output ────────────────────────────────────────────────────
    if needs_meta and "meta" in data:
        meta = data["meta"]
        if meta.get("meta_title"):
            suggestions.append(ContentSuggestion(
                page_url=url,
                wp_post_id=page.get("wp_post_id"),
                post_type=page.get("post_type", "page"),
                suggestion_type="meta_title",
                field_name="seo_title",
                old_value=current_title,
                new_value=meta["meta_title"],
                wp_meta_key=_AI_TITLE_KEY,
                rationale=meta.get("rationale", ""),
                risk_level="low",
                impact_score=75.0,
            ))
        if meta.get("meta_description"):
            suggestions.append(ContentSuggestion(
                page_url=url,
                wp_post_id=page.get("wp_post_id"),
                post_type=page.get("post_type", "page"),
                suggestion_type="meta_description",
                field_name="meta_description",
                old_value=current_meta,
                new_value=meta["meta_description"],
                wp_meta_key=_AI_DESC_KEY,
                rationale=meta.get("rationale", ""),
                risk_level="low",
                impact_score=70.0,
            ))
        if meta.get("focus_keyword"):
            suggestions.append(ContentSuggestion(
                page_url=url,
                wp_post_id=page.get("wp_post_id"),
                post_type=page.get("post_type", "page"),
                suggestion_type="focus_keyword",
                field_name="focus_keyword",
                old_value=None,
                new_value=meta["focus_keyword"],
                wp_meta_key=_AI_KW_KEY,
                rationale="Set primary focus keyword in ai_focus_keyword meta field",
                risk_level="low",
                impact_score=50.0,
            ))

    # ── Parse schema output ──────────────────────────────────────────────────
    if needs_schema and "schema" in data:
        schema_data = data["schema"]
        json_ld = schema_data.get("json_ld", {})
        if json_ld:
            suggestions.append(ContentSuggestion(
                page_url=url,
                wp_post_id=page.get("wp_post_id"),
                post_type=page.get("post_type", "page"),
                suggestion_type="schema",
                field_name="schema_markup",
                old_value=None,
                new_value=f'<script type="application/ld+json">\n{json.dumps(json_ld, indent=2)}\n</script>',
                wp_meta_key=None,
                rationale=schema_data.get("rationale", ""),
                risk_level="medium",
                impact_score=55.0,
            ))

    # ── Parse rewrite output ─────────────────────────────────────────────────
    if needs_rewrite and "rewrite" in data:
        rewrite = data["rewrite"]
        html_block = rewrite.get("html", "")
        if html_block:
            suggestions.append(ContentSuggestion(
                page_url=url,
                wp_post_id=page.get("wp_post_id"),
                post_type=page.get("post_type", "page"),
                suggestion_type="content_rewrite",
                field_name="content",
                old_value=rewrite.get("target_section"),
                new_value=html_block,
                wp_meta_key=None,
                rationale=rewrite.get("rationale", ""),
                risk_level="high",
                impact_score=60.0,
            ))

    return suggestions


# async def _generate_new_page(gap: dict[str, Any]) -> list[ContentSuggestion]:
#     """Generate full content for a new landing page."""
#     response = await _llm_page.ainvoke(
#         [
#             _SYSTEM_PROMPT,
#             HumanMessage(
#                 content=f"""Create a complete SEO-optimized blog post/page for this content gap.
#
# Topic: {gap.get('topic', '')}
# Suggested Title: {gap.get('suggested_title', '')}
# Target Keyword: {gap.get('target_keyword', '')}
# Rationale: {gap.get('rationale', '')}
#
# Generate:
# 1. A compelling H1 title
# 2. SEO meta title (55 chars max)
# 3. SEO meta description (155 chars max)
# 4. Full HTML content (minimum 600 words) with proper H2/H3 structure,
#    internal linking placeholders [INTERNAL_LINK], and natural keyword usage
#
# Respond with JSON:
# {{
#   "title": "H1 title",
#   "meta_title": "SEO title",
#   "meta_description": "meta desc",
#   "slug": "url-slug",
#   "content": "<h2>...</h2><p>...</p>...",
#   "focus_keyword": "target keyword"
# }}"""
#             ),
#         ]
#     )
#     accumulate_token_usage(response)
#
#     try:
#         content = response.content if hasattr(response, "content") else str(response)
#         content = content.strip().lstrip("```json").lstrip("```").rstrip("```")
#         page_data = json.loads(content)
#
#         return [
#             ContentSuggestion(
#                 page_url="",  # New page — no existing URL
#                 wp_post_id=None,
#                 post_type="post",
#                 suggestion_type="new_page",
#                 field_name="new_content",
#                 old_value=None,
#                 new_value=json.dumps(page_data),
#                 wp_meta_key=None,
#                 rationale=gap.get("rationale", ""),
#                 risk_level="medium",
#                 impact_score=80.0,
#             )
#         ]
#     except (json.JSONDecodeError, KeyError):
#         return []


def _build_execution_tasks(
    suggestions: list[ContentSuggestion],
) -> list[ExecutionTask]:
    """Convert ContentSuggestions into ExecutionTask objects."""
    tasks: list[ExecutionTask] = []
    for s in suggestions:
        wp_post_id = s.get("wp_post_id")
        if s["suggestion_type"] != "new_page" and not wp_post_id:
            continue  # Can't apply without a WP ID

        tasks.append(
            ExecutionTask(
                task_id=str(uuid.uuid4()),
                page_url=s["page_url"],
                wp_post_id=wp_post_id or 0,
                post_type=s.get("post_type", "page"),
                change_type=s["suggestion_type"],
                field_name=s["field_name"],
                old_value=s.get("old_value"),
                new_value=s["new_value"],
                wp_meta_key=s.get("wp_meta_key"),
                risk_level=s.get("risk_level", "low"),
                approved=False,
                applied=False,
                error=None,
                snapshot=None,
            )
        )
    return tasks


async def content_generator_node(state: SEOAgentState) -> dict:
    """LangGraph node: generates SEO content improvements and new page content."""

    logger.info("content_generator_agent.start")
    errors: list[str] = list(state.get("errors", []))

    pages = state.get("crawled_pages", [])
    issues = state.get("issues", [])
    keyword_suggestions = state.get("keyword_suggestions", [])
    competitive_insights = state.get("competitive_insights", [])
    prev = state.get("previous_run_context", {})
    recently_changed_pages: set[str] = set(prev.get("pages_changed", []))

    if not pages:
        return {"content_suggestions": [], "execution_tasks": [], "errors": errors}

    # Build keyword map: url -> primary keyword
    kw_map: dict[str, str] = {}
    primary_kws = [kw["keyword"] for kw in keyword_suggestions if kw.get("recommendation") == "primary"]
    if primary_kws:
        for page in pages:
            kw_map[page["url"]] = primary_kws[0]

    # Build page issue index
    issue_index: dict[str, list[SEOIssue]] = {}
    for issue in issues:
        url = issue["page_url"]
        issue_index.setdefault(url, []).append(issue)

    all_suggestions: list[ContentSuggestion] = []

    # ── Build a unified task map: one entry per page, flagging which tasks apply ──
    # This ensures at most ONE LLM call per page regardless of how many task types
    # it needs, rather than separate loops that could call Claude 3x for the same page.
    page_tasks: dict[str, dict] = {}  # url -> {page, needs_meta, needs_schema, needs_rewrite}

    # Meta: top 10 pages by impact score
    pages_with_meta_issues = [
        p for p in pages
        if any(i["category"] == "meta" for i in issue_index.get(p["url"], []))
    ]
    pages_with_meta_issues.sort(
        key=lambda p: sum(i["impact_score"] for i in issue_index.get(p["url"], [])),
        reverse=True,
    )
    for page in pages_with_meta_issues[:10]:
        entry = page_tasks.setdefault(page["url"], {"page": page})
        entry["needs_meta"] = True

    # Schema: first 5 pages missing schema markup
    pages_without_schema = [
        p for p in pages
        if any("MISSING_SCHEMA" in i["rule_id"] for i in issue_index.get(p["url"], []))
    ]
    for page in pages_without_schema[:5]:
        entry = page_tasks.setdefault(page["url"], {"page": page})
        entry["needs_schema"] = True

    # Rewrite: top 5 pages with heading/keyword structure issues, skipping recently changed
    pages_needing_rewrite = [
        p for p in pages
        if p["url"] not in recently_changed_pages
        and any(i["rule_id"] in _REWRITE_RULES for i in issue_index.get(p["url"], []))
    ]
    pages_needing_rewrite.sort(
        key=lambda p: sum(i["impact_score"] for i in issue_index.get(p["url"], [])),
        reverse=True,
    )
    for page in pages_needing_rewrite[:5]:
        entry = page_tasks.setdefault(page["url"], {"page": page})
        entry["needs_rewrite"] = True

    # ── One LLM call per page ────────────────────────────────────────────────
    for url, task_info in page_tasks.items():
        page = task_info["page"]
        primary_kw = kw_map.get(url, page.get("title", "").split()[0] if page.get("title") else "")
        if not primary_kw:
            continue

        if task_info.get("needs_rewrite") and url in recently_changed_pages:
            logger.info("content_generator.skip_recent", url=url, reason="page was changed in the previous run")
            task_info["needs_rewrite"] = False

        try:
            page_suggestions = await _generate_page_improvements(
                page,
                issue_index.get(url, []),
                primary_kw,
                competitive_insights,
                needs_meta=task_info.get("needs_meta", False),
                needs_schema=task_info.get("needs_schema", False),
                needs_rewrite=task_info.get("needs_rewrite", False),
            )
            all_suggestions.extend(page_suggestions)
        except Exception as exc:
            errors.append(f"Content gen error for {url}: {exc}")

    # ── Deterministic canonical fixes (no LLM needed) ────────────────────────
    for page in pages:
        page_rule_ids = {i["rule_id"] for i in issue_index.get(page["url"], [])}
        if "MISSING_CANONICAL" in page_rule_ids and "NOINDEX_SET" not in page_rule_ids:
            all_suggestions.append(ContentSuggestion(
                page_url=page["url"],
                wp_post_id=page.get("wp_post_id"),
                post_type=page.get("post_type", "page"),
                suggestion_type="canonical_url",
                field_name="canonical_url",
                old_value=None,
                new_value=page["url"],
                wp_meta_key="ai_canonical_url",
                rationale="Page is missing a canonical tag — setting self-referencing canonical.",
                risk_level="low",
                impact_score=70.0,
            ))

    # ── Deduplicate new_section / content_rewrite suggestions ────────────────
    # seen_headings: dict[str, list[str]] = {}
    # deduped_suggestions: list[ContentSuggestion] = []
    # for s in all_suggestions:
    #     if s["suggestion_type"] not in ("new_section", "content_rewrite"):
    #         deduped_suggestions.append(s)
    #         continue
    #     from bs4 import BeautifulSoup as _BS2
    #     _h = _BS2(s["new_value"], "lxml").find(["h1", "h2", "h3"])
    #     heading_text = _h.get_text(strip=True).lower() if _h else ""
    #     key = s["page_url"]
    #     bucket = seen_headings.setdefault(key, [])
    #     new_words = set(w for w in re.findall(r"[a-zA-Z]{3,}", heading_text) if len(w) > 3)
    #     duplicate = False
    #     for existing_heading in bucket:
    #         ex_words = set(w for w in re.findall(r"[a-zA-Z]{3,}", existing_heading) if len(w) > 3)
    #         union = new_words | ex_words
    #         if union and len(new_words & ex_words) / len(union) >= 0.5:
    #             duplicate = True
    #             break
    #     if not duplicate:
    #         bucket.append(heading_text)
    #         deduped_suggestions.append(s)
    #     else:
    #         logger.info("content_generator.suggestion_deduped", page_url=s["page_url"], heading=heading_text)
    # all_suggestions = deduped_suggestions

    # ── Build execution tasks ─────────────────────────────────────────────────
    execution_tasks = _build_execution_tasks(all_suggestions)
    execution_tasks = execution_tasks[: settings.max_batch_size]

    logger.info(
        "content_generator_agent.complete",
        suggestions=len(all_suggestions),
        tasks=len(execution_tasks),
    )

    print(
        f"################################\n"
        f"Content Generator complete. Generated {len(all_suggestions)} content improvement suggestions "
        f"with tasks: {execution_tasks} ready for execution. "
        f"Major errors encountered: {errors}. "
        f"Content suggestions for the page: {all_suggestions}\n"
        f"################################"
    )

    return {
        "content_suggestions": all_suggestions,
        "execution_tasks": execution_tasks,
        "errors": errors,
        "messages": [
            HumanMessage(
                content=f"Content generation complete. {len(all_suggestions)} improvements generated, "
                f"{len(execution_tasks)} execution tasks ready for approval."
            )
        ],
    }
