"""Shared LangGraph state for the full SEO agent pipeline."""

from __future__ import annotations

from typing import Annotated, Any, Literal, Optional
from typing_extensions import TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


# ── Sub-state data classes (TypedDicts) ──────────────────────────────────────

class CrawledPage(TypedDict):
    """Result of crawling one page."""
    url: str
    wp_post_id: Optional[int]
    post_type: str                  # post | page
    title: str
    html: str
    text: str
    links: list[str]
    status_code: int
    load_time_ms: int
    word_count: int
    error: Optional[str]


class SEOIssue(TypedDict):
    """An SEO problem found during audit."""
    rule_id: str
    category: str                   # meta | content | technical | structure | performance | schema
    severity: str                   # critical | high | medium | low
    title: str
    description: str
    current_value: Optional[str]
    suggested_fix: Optional[str]
    impact_score: float             # 0–100
    page_url: str
    wp_post_id: Optional[int]
    post_type: str


class KeywordSuggestion(TypedDict):
    """A keyword recommended for a page."""
    keyword: str
    monthly_volume: Optional[int]
    difficulty: Optional[float]
    intent: str                     # informational | transactional | navigational
    recommendation: str             # primary | secondary | long_tail


class ContentSuggestion(TypedDict):
    """AI-generated content improvement for a page."""
    page_url: str
    wp_post_id: Optional[int]
    post_type: str
    suggestion_type: str            # meta_title | meta_description | content_rewrite | new_section | schema
    field_name: str
    old_value: Optional[str]
    new_value: str
    wp_meta_key: Optional[str]      # Yoast meta key if applicable
    rationale: str
    risk_level: str                 # low | medium | high
    impact_score: float


class ExecutionTask(TypedDict):
    """A concrete change task ready to be applied."""
    task_id: str
    page_url: str
    wp_post_id: int
    post_type: str
    change_type: str
    field_name: str
    old_value: Optional[str]
    new_value: str
    wp_meta_key: Optional[str]
    risk_level: str
    approved: bool
    applied: bool
    error: Optional[str]
    snapshot: Optional[dict]


class RankingData(TypedDict):
    """Ranking information for a keyword/page pair."""
    page_url: str
    keyword: str
    position: Optional[int]
    impressions: Optional[int]
    clicks: Optional[int]
    ctr: Optional[float]


# ── Master State ──────────────────────────────────────────────────────────────

class SEOAgentState(TypedDict):
    """Full pipeline state flowing through all LangGraph nodes.

    Convention:
    - Fields prefixed with the agent that writes them.
    - ``messages`` uses LangGraph's add_messages reducer for append-only behavior.
    - All list fields are replaced (not appended) by each agent.
    """

    # ── Run metadata ─────────────────────────────────────────────────────────
    run_id: str
    site_id: int
    target_url: str
    mode: Literal["dry_run", "preview", "live"]

    # ── LLM message history (append-only via reducer) ─────────────────────────
    messages: Annotated[list[BaseMessage], add_messages]

    # ── Crawler output ───────────────────────────────────────────────────────
    crawled_pages: list[CrawledPage]
    crawl_stats: dict[str, Any]      # pages_found, pages_crawled, pages_failed, duration_s

    # ── SEO Audit output ──────────────────────────────────────────────────────
    audit_results: list[dict[str, Any]]   # per-page audit detail
    issues: list[SEOIssue]
    overall_seo_score: float              # 0–100

    # ── Keyword Planner output ────────────────────────────────────────────────
    keyword_suggestions: list[KeywordSuggestion]
    content_gaps: list[dict[str, Any]]    # topics/pages missing from site

    # ── SERP Analysis output ──────────────────────────────────────────────────
    competitive_insights: list[dict[str, Any]]   # per-keyword competitor signals

    # ── Content Generator output ──────────────────────────────────────────────
    content_suggestions: list[ContentSuggestion]
    execution_tasks: list[ExecutionTask]  # tasks built from suggestions

    # ── Human approval ────────────────────────────────────────────────────────
    approval_status: Literal["pending", "approved", "rejected"]
    approval_notes: str
    plan_id: Optional[int]               # DB ExecutionPlan.id

    # ── Fix Executor output ───────────────────────────────────────────────────
    applied_changes: list[ExecutionTask]
    failed_changes: list[ExecutionTask]

    # ── Validation output ──────────────────────────────────────────────────────
    validation_results: list[dict[str, Any]]
    rollback_triggered: list[str]         # task_ids that were rolled back

    # ── Ranking Monitor output ────────────────────────────────────────────────
    ranking_data: list[RankingData]
    performance_report: dict[str, Any]

    # ── Previous run context ─────────────────────────────────────────────────
    # Loaded from the last completed run for this site. Gives agents memory
    # of what was audited, changed, and scored in the prior cycle.
    previous_run_context: dict[str, Any]

    # ── Crawl scope ───────────────────────────────────────────────────────────
    max_pages: Optional[int]              # None=use settings default, 0=full site, N=limit to N

    # ── Flow control ──────────────────────────────────────────────────────────
    errors: list[str]
    next_node: str                        # used by conditional edges
