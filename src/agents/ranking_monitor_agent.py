"""Ranking Monitor Agent — tracks SERP positions and generates performance reports."""

from __future__ import annotations

import json
from typing import Any

import structlog
from langchain_anthropic import ChatAnthropic  # noqa: F401  # production provider
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage

from src.agents.state import RankingData, SEOAgentState
from src.config import get_settings
from src.tools.serp_tools import check_serp_position, get_search_console_data

settings = get_settings()
logger = structlog.get_logger(__name__)


# ── LLM — switch comment blocks to swap provider ──────────────────────────────
# Production (Claude):
_llm = ChatAnthropic(
    model=settings.claude_model,
    temperature=0.4,
    max_tokens=1024,
    anthropic_api_key=settings.anthropic_api_key,
)
# Local testing (OpenAI):
# _llm = ChatOpenAI(
#     model=settings.openai_model,
#     temperature=0.4,
#     max_tokens=settings.openai_max_tokens,
#     api_key=settings.openai_api_key,
# )

_SYSTEM_PROMPT = SystemMessage(content="""You are the Ranking Monitor Agent for an SEO system.
Your role is to:
1. Analyze ranking data across all tracked keywords
2. Identify which pages improved, declined, or maintained positions
3. Correlate ranking changes with recent SEO changes applied
4. Highlight quick-win opportunities (positions 4-10 that could reach top 3)
5. Generate actionable recommendations for the next optimization cycle
6. Produce a clear executive summary for the site owner

Always contextualize data: rising/falling trends, seasonal factors, and competitive landscape.""")


async def ranking_monitor_node(state: SEOAgentState) -> dict:
    """LangGraph node: checks rankings, pulls GSC data, generates performance report."""

    logger.info("ranking_monitor_agent.start")
    errors: list[str] = list(state.get("errors", []))

    pages = state.get("crawled_pages", [])
    keyword_suggestions = state.get("keyword_suggestions", [])
    issues = state.get("issues", [])
    applied_changes = state.get("applied_changes", [])
    validation_results = state.get("validation_results", [])

    ranking_data: list[RankingData] = []

    # ── SERP position checks ────────────────────────────────────────────────
    # Build page→keyword mapping
    page_keyword_pairs: list[tuple[str, str]] = []
    for page in pages[:20]:  # Limit API calls
        url = page.get("url", "")
        if not url:
            continue

        # Use primary keyword from suggestions, fall back to page title words
        primary_kw = None
        for kw in keyword_suggestions:
            if kw.get("recommendation") == "primary" and url:
                primary_kw = kw["keyword"]
                break

        if not primary_kw and page.get("title"):
            # Use first 3 meaningful words as keyword
            words = [w for w in page["title"].split() if len(w) > 3][:3]
            primary_kw = " ".join(words)

        if primary_kw:
            page_keyword_pairs.append((url, primary_kw))

    for url, keyword in page_keyword_pairs[:10]:  # Max 10 SERP checks per run
        try:
            serp_result = await check_serp_position.ainvoke(
                {"keyword": keyword, "target_url": url}
            )
            ranking_data.append(
                RankingData(
                    page_url=url,
                    keyword=keyword,
                    position=serp_result.get("position"),
                    impressions=None,
                    clicks=None,
                    ctr=None,
                )
            )
        except Exception as exc:
            errors.append(f"SERP check error for {url}/{keyword}: {exc}")

    # ── Google Search Console data ─────────────────────────────────────────
    gsc_data = await get_search_console_data.ainvoke(
        {"site_url": state["target_url"], "days": 28}
    )

    # ── Generate performance report with Claude ────────────────────────────
    ranking_summary = []
    for rd in ranking_data:
        pos = rd.get("position")
        pos_text = f"Position #{pos}" if pos else "Not in top 100"
        ranking_summary.append(f"- {rd['keyword']}: {pos_text} ({rd['page_url']})")

    applied_summary = [
        f"- {c['change_type']} on {c['page_url']}: {'applied' if c.get('applied') else 'failed'}"
        for c in applied_changes[:10]
    ]


    overall_score = state.get("overall_seo_score", 0)

    report_prompt = f"""SEO run summary for {state['target_url']}.
Stats: {len(pages)} pages, score {overall_score}/100, {len(issues)} issues, {len(applied_changes)} changes applied.
Rankings: {', '.join(ranking_summary[:5]) or 'none'}
Changes: {', '.join(applied_summary[:5]) or 'none'}
GSC: {json.dumps(gsc_data.get('averages', {}))}

Write a brief report with 4 sections (2-3 sentences each):
1. Executive Summary
2. Key Wins
3. Still Needs Attention
4. Top 3 Next Actions"""

    llm_response = await _llm.ainvoke(
        [_SYSTEM_PROMPT, HumanMessage(content=report_prompt)]
    )
    report_text = llm_response.content if hasattr(llm_response, "content") else str(llm_response)

    performance_report: dict[str, Any] = {
        "site_url": state["target_url"],
        "run_id": state.get("run_id", ""),
        "pages_crawled": len(pages),
        "issues_found": len(issues),
        "overall_seo_score": overall_score,
        "changes_applied": len(applied_changes),
        "validation_passed": sum(1 for v in validation_results if v.get("passed")),
        "validation_failed": sum(1 for v in validation_results if not v.get("passed")),
        "rollbacks": len(state.get("rollback_triggered", [])),
        "ranking_data": ranking_data,
        "gsc_averages": gsc_data.get("averages", {}),
        "ai_report": report_text,
        "keyword_rankings": {
            rd["keyword"]: rd.get("position") for rd in ranking_data
        },
    }

    logger.info(
        "ranking_monitor_agent.complete",
        rankings_checked=len(ranking_data),
    )

    print(f"################################\nRanking Monitor complete. Checked rankings for {len(ranking_data)} keywords. the performance report generated is: {performance_report} ################################")

    return {
        "ranking_data": ranking_data,
        "performance_report": performance_report,
        "errors": errors,
        "messages": [
            HumanMessage(
                content=f"Ranking Monitor complete. {len(ranking_data)} keywords tracked.\n\n"
                f"Performance Report:\n{report_text[:500]}..."
            )
        ],
    }
