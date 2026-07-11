"""Keyword Planner Agent — researches and assigns keywords to pages."""

from __future__ import annotations

import json
from typing import Any

import structlog
from langchain_anthropic import ChatAnthropic  # noqa: F401  # production provider
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage

from src.agents.state import KeywordSuggestion, SEOAgentState
from src.config import get_settings
from src.observability.metrics import accumulate_token_usage
from src.tools.keyword_tools import (
    analyze_keyword_density,
    extract_keywords_from_content,
)

settings = get_settings()
logger = structlog.get_logger(__name__)


# ── LLM — switch comment blocks to swap provider ──────────────────────────────
# Production (Claude):
_llm = ChatAnthropic(
    model=settings.claude_model,
    temperature=0.4,
    max_tokens=8192,
    anthropic_api_key=settings.anthropic_api_key,
)
# Local testing (OpenAI):
# _llm = ChatOpenAI(
#     model=settings.openai_model,
#     temperature=0.4,
#     max_tokens=settings.openai_max_tokens,
#     api_key=settings.openai_api_key,
# )

_SYSTEM_PROMPT = SystemMessage(content="""You are an expert SEO Keyword Research Strategist.
Focus on search intent alignment, long-tail opportunities, and content gaps.
Return ONLY valid JSON — no markdown, no extra text.""")


# ── Node function ─────────────────────────────────────────────────────────────

async def keyword_planner_node(state: SEOAgentState) -> dict:
    """LangGraph node: keyword research and gap analysis."""

    logger.info("keyword_planner_agent.start")
    errors: list[str] = list(state.get("errors", []))

    pages = state.get("crawled_pages", [])
    issues = state.get("issues", [])

    if not pages:
        return {"keyword_suggestions": [], "content_gaps": [], "errors": errors}

    # ── Step 1: Direct keyword extraction per page ────────────────────────────
    page_keyword_profiles: list[dict[str, Any]] = []

    for page in pages[:50]:  # cap for performance
        if not page.get("text") or len(page["text"]) < 100:
            continue
        try:
            kw_data: dict = extract_keywords_from_content.invoke(
                {"text": page["text"][:1500], "top_n": 15}
            )
            if not isinstance(kw_data, dict):
                kw_data = {}

            # Analyze density for top keywords found
            top_kws = [k["keyword"] for k in kw_data.get("top_keywords", [])[:5]]
            density_results: list[dict] = []
            for kw in top_kws:
                d = analyze_keyword_density.invoke(
                    {"text": page["text"][:1500], "keyword": kw}
                )
                if isinstance(d, dict):
                    density_results.append(d)

            page_keyword_profiles.append({
                "url": page["url"],
                "wp_post_id": page.get("wp_post_id"),
                "post_type": page.get("post_type", "page"),
                "title": page.get("title", ""),
                "top_keywords": kw_data.get("top_keywords", [])[:10],
                "top_bigrams": kw_data.get("top_bigrams", [])[:5],
                "word_count": page.get("word_count", 0),
            })

        except Exception as exc:
            errors.append(f"Keyword extraction error for {page['url']}: {exc}")

    # ── Step 2: Ask Claude for full keyword strategy across all pages ─────────
    profiles_text = json.dumps(
        [
            {
                "url": p["url"],
                "title": p["title"],
                "top_keywords": [k["keyword"] for k in p["top_keywords"][:3]],
                "word_count": p["word_count"],
            }
            for p in page_keyword_profiles[:10]
        ],
        indent=2,
    )

    keyword_issues = [
        i for i in issues if i["category"] == "content" or "KEYWORD" in i["rule_id"]
    ]
    issues_text = "\n".join(
        f"- {i['page_url']}: {i['title']}" for i in keyword_issues[:10]
    )

    llm_response = await _llm.ainvoke(
        [
            _SYSTEM_PROMPT,
            HumanMessage(
                content=f"""Analyze these pages from {state['target_url']} and provide a keyword strategy.

PAGE PROFILES:
{profiles_text}

KEYWORD ISSUES FROM AUDIT:
{issues_text or 'None found'}

Please respond with a JSON object in this exact format:
{{
  "keyword_assignments": [
    {{
      "url": "page url",
      "primary_keyword": "main keyword",
      "secondary_keywords": ["kw1", "kw2"],
      "long_tail_keywords": ["long tail 1", "long tail 2"],
      "intent": "informational|transactional|navigational",
      "recommendation": "brief explanation"
    }}
  ],
  "content_gaps": [
    {{
      "topic": "missing topic",
      "suggested_title": "page title",
      "target_keyword": "main keyword",
      "rationale": "why this page would rank and bring traffic",
      "estimated_monthly_searches": "low|medium|high"
    }}
  ],
  "cannibalization_risks": [
    {{
      "keyword": "competing keyword",
      "competing_urls": ["url1", "url2"],
      "recommendation": "consolidate|differentiate"
    }}
  ]
}}

Return ONLY the JSON, no other text."""
            ),
        ]
    )
    accumulate_token_usage(llm_response)

    # ── Step 3: Parse LLM response ────────────────────────────────────────────
    keyword_suggestions: list[KeywordSuggestion] = []
    content_gaps: list[dict[str, Any]] = []

    try:
        content = llm_response.content if hasattr(llm_response, "content") else str(llm_response)
        content = content.strip()
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        kw_plan = json.loads(content)

        for assignment in kw_plan.get("keyword_assignments", []):
            if assignment.get("primary_keyword"):
                keyword_suggestions.append(
                    KeywordSuggestion(
                        keyword=assignment["primary_keyword"],
                        monthly_volume=None,
                        difficulty=None,
                        intent=assignment.get("intent", "informational"),
                        recommendation="primary",
                    )
                )
            for kw in assignment.get("secondary_keywords", []):
                keyword_suggestions.append(
                    KeywordSuggestion(
                        keyword=kw,
                        monthly_volume=None,
                        difficulty=None,
                        intent=assignment.get("intent", "informational"),
                        recommendation="secondary",
                    )
                )
            for kw in assignment.get("long_tail_keywords", []):
                keyword_suggestions.append(
                    KeywordSuggestion(
                        keyword=kw,
                        monthly_volume=None,
                        difficulty=None,
                        intent="informational",
                        recommendation="long_tail",
                    )
                )

        content_gaps = kw_plan.get("content_gaps", [])

    except (json.JSONDecodeError, KeyError) as exc:
        errors.append(f"Keyword planner JSON parse error: {exc}")
        logger.warning("keyword_planner_agent.parse_error", error=str(exc))

    # Deduplicate
    seen_kws: set[str] = set()
    unique_suggestions: list[KeywordSuggestion] = []
    for kw in keyword_suggestions:
        if kw["keyword"] not in seen_kws:
            seen_kws.add(kw["keyword"])
            unique_suggestions.append(kw)

    logger.info(
        "keyword_planner_agent.complete",
        keywords_found=len(unique_suggestions),
        content_gaps=len(content_gaps),
    )

    print(f"################################ \nKeyword Planner complete. {len(unique_suggestions)} keyword suggestions and {len(content_gaps)} content gaps identified. the suggested keywords: {unique_suggestions} and the content gaps is identified as: {content_gaps} and the obtained major errors: {errors} ################################")

    return {
        "keyword_suggestions": unique_suggestions,
        "content_gaps": content_gaps,
        "errors": errors,
        "messages": [
            HumanMessage(
                content=f"Keyword planning complete. {len(unique_suggestions)} keyword suggestions. "
                f"{len(content_gaps)} content gap opportunities identified."
            )
        ],
    }
