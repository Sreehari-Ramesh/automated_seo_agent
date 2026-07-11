"""Streamlit UI for the Automated SEO Agent."""

from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import requests
import streamlit as st

# ── Config ────────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="SEO Agent",
    page_icon="🔍",
    layout="wide",
    initial_sidebar_state="expanded",
)

def _deploy_value(key: str, fallback: str = "") -> str:
    try:
        from src.config.deployment_config import DOCKER_ENV  # type: ignore
        val = str(DOCKER_ENV.get(key, "")).strip()
        if val:
            return val
    except Exception:
        pass
    return fallback


# When deployed, set SEO_AGENT_API_URL in deployment_config or env if needed.
_default_api = "http://api:8080" if Path("/.dockerenv").exists() else "http://localhost:8080"
API_BASE = os.environ.get("SEO_AGENT_API_URL", _deploy_value("SEO_AGENT_API_URL", _default_api)).rstrip("/")
API_SECRET = os.environ.get("SEO_AGENT_API_SECRET", _deploy_value("SEO_AGENT_API_SECRET", "")).strip()
API_TIMEOUT = int(os.environ.get("SEO_AGENT_API_TIMEOUT", _deploy_value("SEO_AGENT_API_TIMEOUT", "30")))

STATUS_COLORS = {
    "pending": "#6c757d",
    "running": "#0d6efd",
    "waiting_for_approval": "#fd7e14",
    "completed": "#198754",
    "failed": "#dc3545",
    "cancelled": "#6c757d",
    "rejected": "#dc3545",
}

SEVERITY_COLORS = {
    "critical": "#dc3545",
    "high": "#fd7e14",
    "medium": "#ffc107",
    "low": "#198754",
}

RISK_COLORS = {
    "high": "#dc3545",
    "medium": "#fd7e14",
    "low": "#198754",
}

PIPELINE_NODES = [
    "crawler",
    "seo_audit",
    "keyword_planner",
    "serp_analysis",
    "content_generator",
    "policy_gate",
    "human_review",
    "fix_executor",
    "validator",
    "ranking_monitor",
]

# ── Session state defaults ────────────────────────────────────────────────────

def _init_state() -> None:
    defaults = {
        "current_run_id": None,
        "auto_refresh": False,
        "last_refresh": 0.0,
        "active_tab": "Dashboard",
    }
    for key, val in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = val


def _set_current_run(run_id: str | None, *, auto_refresh: bool | None = None) -> None:
    """Set active run in session state and keep it in URL query params."""
    st.session_state.current_run_id = run_id
    if auto_refresh is not None:
        st.session_state.auto_refresh = auto_refresh

    try:
        if run_id:
            st.query_params["run_id"] = run_id
        else:
            st.query_params.pop("run_id", None)
    except Exception:
        # Query params support may vary by Streamlit runtime; session state still works.
        pass


def _rehydrate_current_run() -> None:
    """Recover active run after browser refresh."""
    if st.session_state.current_run_id:
        return

    # 1) Prefer explicit run_id in URL.
    run_id = None
    try:
        qp = st.query_params.get("run_id")
        if isinstance(qp, list):
            run_id = qp[0] if qp else None
        else:
            run_id = qp
    except Exception:
        run_id = None

    if run_id:
        data = _get(f"/api/v1/pipeline/{run_id}")
        if data and data.get("run_id"):
            _set_current_run(str(data["run_id"]), auto_refresh=data.get("status") in ("pending", "running"))
            return

    # 2) Fallback to latest active run from API.
    for status in ("running", "pending", "waiting_for_approval"):
        runs = _get("/api/v1/runs", status=status, limit=1) or []
        if runs:
            latest = runs[0]
            latest_run_id = latest.get("run_id")
            if latest_run_id:
                _set_current_run(str(latest_run_id), auto_refresh=status in ("running", "pending"))
                return


# ── API helpers ───────────────────────────────────────────────────────────────

def _api(method: str, path: str, **kwargs) -> dict | list | None:
    try:
        headers = dict(kwargs.pop("headers", {}) or {})
        if API_SECRET:
            headers.setdefault("Authorization", f"Bearer {API_SECRET}")
        resp = requests.request(method, f"{API_BASE}{path}", timeout=API_TIMEOUT, headers=headers, **kwargs)
        if resp.status_code in (200, 201, 202):
            return resp.json()
        st.error(f"API error {resp.status_code}: {resp.text[:200]}")
        return None
    except requests.ReadTimeout:
        st.error(
            f"API request timed out after {API_TIMEOUT}s: {method} {path}. "
            "If this persists, verify the API server logs and current run load."
        )
        return None
    except requests.ConnectionError:
        st.error("Cannot reach the API server. Make sure FastAPI is running on port 8080.")
        return None
    except Exception as exc:
        st.error(f"Request failed: {exc}")
        return None


def _get(path: str, **params) -> dict | list | None:
    return _api("GET", path, params=params)


def _post(path: str, payload: dict) -> dict | None:
    return _api("POST", path, json=payload)


# ── UI helpers ────────────────────────────────────────────────────────────────

def _status_badge(status: str) -> str:
    color = STATUS_COLORS.get(status, "#6c757d")
    return f'<span style="background:{color};color:#fff;padding:2px 10px;border-radius:12px;font-size:12px;font-weight:600">{status.replace("_", " ").upper()}</span>'


def _risk_badge(level: str) -> str:
    color = RISK_COLORS.get(level, "#6c757d")
    return f'<span style="background:{color};color:#fff;padding:1px 8px;border-radius:10px;font-size:11px;font-weight:700">{level.upper()}</span>'


def _metric_card(label: str, value: Any, delta: str = "") -> None:
    st.metric(label=label, value=value, delta=delta or None)


def _check_api_health() -> bool:
    try:
        resp = requests.get(f"{API_BASE}/health", timeout=5)
        return resp.status_code == 200
    except Exception:
        return False


# ── Sidebar ───────────────────────────────────────────────────────────────────

def render_sidebar() -> tuple[str, str, int]:
    with st.sidebar:
        st.title("SEO Agent")
        st.markdown("---")

        # API health indicator
        healthy = _check_api_health()
        if healthy:
            st.success("API Online", icon="✅")
        else:
            st.error("API Offline", icon="❌")

        st.markdown("---")

        # Mode selector
        st.subheader("Execution Mode")
        mode_display = st.radio(
            "Mode",
            options=[
                "Test Only (No Changes)",
                "Preview (Approval Required)",
                "Live (Direct Publish)",
            ],
            label_visibility="collapsed",
            help=(
                "Test Only: analysis + simulation only. "
                "Preview: prepares changes and waits for approval. "
                "Live: publishes changes directly without approval pause."
            ),
        )
        mode_map = {
            "Test Only (No Changes)": "dry_run",
            "Preview (Approval Required)": "preview",
            "Live (Direct Publish)": "live",
        }
        mode = mode_map[mode_display]
        auto_approve = mode == "live"

        if mode == "live":
            st.warning("Live mode will write changes directly to WordPress (no approval step).")

        st.markdown("---")

        # Site selector
        st.subheader("Target Site")
        sites_data = _get("/api/v1/sites") or []
        site_options = {s["name"]: s for s in sites_data} if sites_data else {}

        target_url = ""
        site_id = 1

        if site_options:
            selected_site_name = st.selectbox("Select site", list(site_options.keys()))
            selected_site = site_options[selected_site_name]
            target_url = selected_site["base_url"]
            site_id = selected_site["id"]
            st.caption(f"`{target_url}`")
        else:
            target_url = st.text_input(
                "Site URL",
                placeholder="https://example.com",
                help="No sites configured yet. Enter a URL to run the pipeline.",
            )
            site_id = 1

        st.markdown("---")

        # Pages to analyse
        st.subheader("Pages to Analyse")
        _PAGE_LIMIT_OPTIONS = {
            "Full Site (no limit)": 0,
            "10 pages": 10,
            "25 pages": 25,
            "30 pages": 30,
            "35 pages": 35,
            "40 pages": 40,
            "50 pages": 50,
        }
        page_limit_label = st.selectbox(
            "Number of pages",
            options=list(_PAGE_LIMIT_OPTIONS.keys()),
            index=0,
            help="Limit how many pages are crawled and analysed in this run.",
            label_visibility="collapsed",
        )
        max_pages: int | None = _PAGE_LIMIT_OPTIONS[page_limit_label]
        if max_pages == 0:
            st.caption("All pages will be crawled.")
        else:
            st.caption(f"Only the first {max_pages} discovered pages will be analysed.")

        st.markdown("---")

        # Manual trigger
        st.subheader("Manual Trigger")
        trigger_disabled = not healthy or not target_url
        if st.button(
            "Run Pipeline Now",
            type="primary",
            use_container_width=True,
            disabled=trigger_disabled,
        ):
            payload = {
                "target_url": target_url,
                "site_id": site_id,
                "mode": mode,
                "max_pages": max_pages,
                "auto_approve": auto_approve,
            }
            result = _post("/api/v1/pipeline/run", payload)
            if result:
                _set_current_run(result["run_id"], auto_refresh=True)
                queue_status = result.get("queue_status", "unknown")
                if queue_status == "enqueued":
                    st.success(f"Pipeline queued! Run ID: `{result['run_id'][:8]}…`")
                elif queue_status == "in_process_fallback":
                    st.warning(
                        "Celery is unavailable, so this run is executing inside the API process. "
                        "Keep the API server running until completion."
                    )
                else:
                    st.warning(
                        f"Run created (`{result['run_id'][:8]}…`) but queue status is `{queue_status}`. "
                        "If it stays pending, verify Redis + Celery worker."
                    )
                time.sleep(1)
                st.rerun()

        if st.session_state.current_run_id:
            st.caption(f"Active run: `{st.session_state.current_run_id[:12]}…`")

    return mode, target_url, site_id


# ── Tab: Dashboard ────────────────────────────────────────────────────────────

def render_dashboard() -> None:
    st.header("Dashboard")

    data = _get("/api/v1/dashboard")
    if not data:
        st.info("No data available yet. Trigger a pipeline run to get started.")
        return

    # Top metrics
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        _metric_card("Total Pages", data.get("total_pages", 0))
    with col2:
        _metric_card("Avg SEO Score", f"{data.get('avg_seo_score', 0):.1f} / 100")
    with col3:
        _metric_card("Open Issues", data.get("open_issues", 0))
    with col4:
        issues_by_sev = data.get("issues_by_severity", {})
        critical = issues_by_sev.get("critical", 0)
        _metric_card("Critical Issues", critical)

    st.markdown("---")

    col_left, col_right = st.columns(2)

    with col_left:
        st.subheader("Pages Needing Attention")
        pages = data.get("pages_needing_attention", [])
        if pages:
            for page in pages:
                score = page.get("seo_score", 0)
                bar_color = "red" if score < 40 else "orange" if score < 70 else "green"
                st.markdown(
                    f"**{page.get('url', 'N/A').split('/')[-1] or 'Home'}**  \n"
                    f"`{page.get('url', '')}`  \n"
                    f"Score: **{score:.0f}/100**"
                )
                st.progress(int(score) / 100)
                st.markdown("")
        else:
            st.info("No pages found.")

    with col_right:
        st.subheader("Recent Runs")
        recent = data.get("recent_runs", [])
        if recent:
            for run in recent:
                status = run.get("status", "")
                started = run.get("started_at", "")[:16].replace("T", " ")
                st.markdown(
                    f"{_status_badge(status)} &nbsp; `{run['run_id'][:8]}…` &nbsp; "
                    f"**{run.get('mode', '')}** &nbsp; {started}",
                    unsafe_allow_html=True,
                )
                st.markdown("")
        else:
            st.info("No recent runs.")

    # Issues breakdown
    if issues_by_sev:
        st.markdown("---")
        st.subheader("Issues by Severity")
        sev_cols = st.columns(4)
        for col, sev in zip(sev_cols, ["critical", "high", "medium", "low"]):
            with col:
                count = issues_by_sev.get(sev, 0)
                color = SEVERITY_COLORS.get(sev, "#6c757d")
                st.markdown(
                    f'<div style="background:{color}22;border-left:4px solid {color};'
                    f'padding:12px;border-radius:4px">'
                    f'<div style="font-size:28px;font-weight:700;color:{color}">{count}</div>'
                    f'<div style="font-size:13px;color:#555">{sev.upper()}</div>'
                    f"</div>",
                    unsafe_allow_html=True,
                )


# ── Tab: Current Run ──────────────────────────────────────────────────────────

def _node_icon(node: str, completed: set, failed: set, current: str) -> str:
    if node in failed:
        return "FAILED"
    if node in completed:
        return "DONE"
    if node == current:
        return "RUNNING"
    return "waiting"


def _render_performance_report(perf: dict) -> None:
    """Render performance_report dict with structured display instead of raw JSON."""
    summary = perf.get("summary") or perf.get("ranking_summary") or {}
    if summary:
        pc1, pc2, pc3 = st.columns(3)
        pc1.metric("Keywords Tracked", summary.get("keywords_tracked", "—"))
        pc2.metric("Avg Position", summary.get("avg_position") or "—")
        pc3.metric("Total Clicks", summary.get("total_clicks") or "—")

    improvements = perf.get("improvements") or perf.get("position_improvements") or []
    declines = perf.get("declines") or perf.get("position_declines") or []
    if improvements:
        with st.expander(f"Position improvements ({len(improvements)})"):
            for item in improvements:
                st.markdown(
                    f"`{item.get('keyword','')}` — {item.get('page_url','')}  \n"
                    f"Position: {item.get('old_position','?')} → {item.get('new_position','?')}"
                )
    if declines:
        with st.expander(f"Position declines ({len(declines)})"):
            for item in declines:
                st.markdown(
                    f"`{item.get('keyword','')}` — {item.get('page_url','')}  \n"
                    f"Position: {item.get('old_position','?')} → {item.get('new_position','?')}"
                )
    # Fallback: raw JSON if structure is unrecognised
    if not summary and not improvements and not declines:
        st.json(perf)


def render_current_run() -> None:
    st.header("Current Run")

    run_id = st.session_state.current_run_id

    if not run_id:
        st.info("No active run. Use the sidebar to trigger the pipeline.")
        return

    col1, col2 = st.columns([3, 1])
    with col1:
        st.caption(f"Run ID: `{run_id}`")
    with col2:
        auto = st.toggle("Auto-refresh (5s)", value=st.session_state.auto_refresh)
        st.session_state.auto_refresh = auto

    # Fetch run + events in one pass each (not per-node)
    data = _get(f"/api/v1/pipeline/{run_id}")
    if not data:
        return

    events: list[dict] = _get(f"/api/v1/pipeline/{run_id}/events") or []

    status = data.get("status", "")
    mode = data.get("mode", "")
    target_url = data.get("target_url", "")
    site_id = data.get("site_id", 1)
    max_pages = data.get("max_pages")
    current_node = data.get("current_node") or ""

    # ── Run control buttons ───────────────────────────────────────────────────
    btn_cols = st.columns(4)
    with btn_cols[0]:
        if status in ("pending", "running"):
            if st.button("⏹ Stop / Cancel", type="secondary", use_container_width=True):
                result = _post(f"/api/v1/pipeline/{run_id}/cancel", {})
                if result:
                    st.warning("Run cancelled.")
                    st.session_state.auto_refresh = False
                    time.sleep(0.5)
                    st.rerun()
    with btn_cols[1]:
        if status in ("failed", "completed", "cancelled"):
            if st.button("🔁 Re-run", type="secondary", use_container_width=True):
                payload = {
                    "target_url": target_url,
                    "site_id": site_id,
                    "mode": mode,
                    "max_pages": max_pages,
                    "auto_approve": mode == "live",
                }
                result = _post("/api/v1/pipeline/run", payload)
                if result:
                    _set_current_run(result["run_id"], auto_refresh=True)
                    st.success(f"New run started: `{result['run_id'][:8]}…`")
                    time.sleep(0.5)
                    st.rerun()
    with btn_cols[2]:
        if status == "waiting_for_approval":
            st.info("Waiting for approval →  Approval tab", icon="⏳")
    with btn_cols[3]:
        if status in ("completed", "failed", "cancelled"):
            if st.button("🗑 Clear", type="secondary", use_container_width=True):
                _set_current_run(None, auto_refresh=False)
                st.rerun()

    completed_nodes = {e["node_name"] for e in events if e["event_type"] == "node_complete"}
    failed_nodes = {e["node_name"] for e in events if e["event_type"] == "node_error"}

    # ── Status line ───────────────────────────────────────────────────────────
    st.markdown(f"**Status:** `{status.upper()}` &nbsp; | &nbsp; **Mode:** `{mode}` &nbsp; | &nbsp; **URL:** {target_url}")

    started_at = data.get("started_at", "")[:19].replace("T", " ")
    ended_at = data.get("ended_at")
    ended_str = ended_at[:19].replace("T", " ") if ended_at else "still running"
    st.caption(f"Started: {started_at}  |  Ended: {ended_str}")

    if data.get("error"):
        st.error(f"Pipeline error: {data['error']}")

    if status == "pending" and not events:
        st.warning(
            "Run is still pending and no node has started yet. "
            "This usually means no Celery worker picked up the job."
        )
        st.caption(
            "Start Redis + Celery worker, or rely on API in-process fallback if enabled."
        )

    if status == "waiting_for_approval":
        st.warning("Pipeline paused — go to the **Approval** tab to review and approve changes.")

    st.markdown("---")

    # ── Node progress as plain text table ────────────────────────────────────
    st.subheader("Node Progress")

    node_rows = []
    for node in PIPELINE_NODES:
        state = _node_icon(node, completed_nodes, failed_nodes, current_node)
        # find matching complete event for duration + tokens
        complete_ev = next(
            (e for e in events if e["node_name"] == node and e["event_type"] == "node_complete"),
            None,
        )
        error_ev = next(
            (e for e in events if e["node_name"] == node and e["event_type"] == "node_error"),
            None,
        )
        ev = complete_ev or error_ev

        duration = f"{ev['duration_ms']} ms" if ev and ev.get("duration_ms") else "—"
        tokens = ev["payload"].get("tokens", {}) if ev else {}
        in_tok = tokens.get("input", 0)
        out_tok = tokens.get("output", 0)
        tok_str = f"{in_tok} in / {out_tok} out" if (in_tok or out_tok) else "—"

        state_label = {
            "DONE": "✅ Done",
            "RUNNING": "⚙️  Running",
            "FAILED": "❌ Failed",
            "waiting": "⬜ Waiting",
        }[state]

        node_rows.append({
            "Node": node.replace("_", " ").title(),
            "State": state_label,
            "Duration": duration,
            "Tokens (in / out)": tok_str,
        })

    st.table(node_rows)

    st.markdown("---")

    # ── Full event log ────────────────────────────────────────────────────────
    st.subheader("Event Log")
    if events:
        log_lines = []
        for ev in events:
            ts = ev.get("timestamp", "")[:19].replace("T", " ")
            node = ev.get("node_name", "")
            etype = ev.get("event_type", "")
            dur = f"  [{ev['duration_ms']} ms]" if ev.get("duration_ms") else ""
            tokens = ev.get("payload", {}).get("tokens", {})
            tok = ""
            if tokens and (tokens.get("input") or tokens.get("output")):
                tok = f"  tokens: {tokens.get('input',0)} in / {tokens.get('output',0)} out"
            err = ev.get("payload", {}).get("error", "")
            err_str = f"  ERROR: {err}" if err else ""
            log_lines.append(f"{ts}  {node:<22} {etype:<18}{dur}{tok}{err_str}")

        st.code("\n".join(log_lines), language=None)
    else:
        st.caption("No events recorded yet.")

    # ── Result summary ────────────────────────────────────────────────────────
    if status == "completed":
        st.markdown("---")
        st.subheader("Result Summary")
        result = data.get("result") or data.get("result_summary") or {}

        if not result:
            st.info("Run completed but no result summary was saved.")
        else:
            # ── Key metrics ───────────────────────────────────────────────────
            r1, r2, r3, r4 = st.columns(4)
            with r1:
                seo_score = result.get("overall_seo_score")
                if seo_score is not None:
                    st.metric("SEO Score", f"{float(seo_score):.1f} / 100")
            with r2:
                st.metric("Issues Found", result.get("issues_found", "—"))
            with r3:
                st.metric("Changes Applied", result.get("changes_applied", "—"))
            with r4:
                st.metric("Changes Failed", result.get("changes_failed", "—"))

            rollbacks = result.get("rollbacks", 0)
            if rollbacks:
                st.warning(f"Rollbacks triggered: {rollbacks}")

            errors = result.get("errors") or []
            if errors:
                st.error("Pipeline errors:\n" + "\n".join(str(e) for e in errors))

            summary_text = result.get("changes_summary", "")
            if summary_text:
                st.info(summary_text)

            # ── Crawl stats ───────────────────────────────────────────────────
            crawl = result.get("crawl_stats") or {}
            if crawl:
                st.markdown("---")
                st.subheader("Crawl Summary")
                cs1, cs2, cs3, cs4, cs5 = st.columns(5)
                cs1.metric("Pages Found", crawl.get("total_wp_content", "—"))
                cs2.metric("Pages Crawled", crawl.get("pages_crawled", "—"))
                cs3.metric("Pages Failed", crawl.get("pages_failed", "—"))
                cs4.metric("Avg Load Time", f"{crawl.get('avg_load_time_ms', 0)} ms")
                cs5.metric("Duration", f"{crawl.get('duration_s', '—')} s")
                slow = crawl.get("slow_pages") or []
                if slow:
                    with st.expander(f"Slow pages (> 3s): {len(slow)}"):
                        for u in slow:
                            st.caption(u)
                err_pages = crawl.get("error_pages") or []
                if err_pages:
                    with st.expander(f"Crawl error pages: {len(err_pages)}"):
                        for u in err_pages:
                            st.caption(u)

            # ── All issues ────────────────────────────────────────────────────
            all_issues = result.get("all_issues") or result.get("top_issues") or []
            if all_issues:
                st.markdown("---")
                st.subheader(f"Issues ({len(all_issues)})")

                sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
                sev_filter = st.selectbox(
                    "Filter by severity",
                    ["All", "critical", "high", "medium", "low"],
                    key="issue_sev_filter",
                )
                filtered = all_issues if sev_filter == "All" else [
                    i for i in all_issues if i.get("severity") == sev_filter
                ]
                for issue in filtered:
                    sev = issue.get("severity", "")
                    color = SEVERITY_COLORS.get(sev, "#6c757d")
                    rule = issue.get("rule_id", "?")
                    cat = issue.get("category", "")
                    title = issue.get("title", rule)
                    page = issue.get("page_url", "")
                    desc = issue.get("description", "")
                    current = issue.get("current_value", "")
                    fix = issue.get("suggested_fix", "")
                    impact = issue.get("impact_score", 0)
                    label = (
                        f'<span style="color:{color};font-weight:700">[{sev.upper()}]</span> '
                        f'`{rule}` &nbsp; <span style="color:#888;font-size:12px">[{cat}] impact: {impact:.0f}</span>'
                        f'<br><small>{page}</small>'
                    )
                    with st.expander(title or rule):
                        st.markdown(label, unsafe_allow_html=True)
                        if desc:
                            st.write(desc)
                        if current:
                            st.markdown(f"**Current:** `{current[:300]}`")
                        if fix:
                            st.markdown(f"**Suggested fix:** {fix}")

            # ── Per-page audit scores ─────────────────────────────────────────
            page_audits = result.get("page_audit_summaries") or []
            if page_audits:
                st.markdown("---")
                st.subheader(f"Per-page Audit Scores ({len(page_audits)})")
                audit_rows = []
                for p in page_audits:
                    psi = p.get("psi") or {}
                    audit_rows.append({
                        "URL": p.get("url", ""),
                        "SEO Score": p.get("seo_score") if p.get("seo_score") is not None else "—",
                        "Issues": p.get("issues_count", 0),
                        "Words": p.get("word_count", 0),
                        "Load (ms)": p.get("load_time_ms", 0),
                        "PSI Perf": psi.get("performance_score", "—"),
                        "PSI SEO": psi.get("seo_score", "—"),
                    })
                st.dataframe(pd.DataFrame(audit_rows), use_container_width=True)

            # ── Keywords & content gaps ───────────────────────────────────────
            kw_suggestions = result.get("keyword_suggestions") or []
            content_gaps = result.get("content_gaps") or []
            if kw_suggestions or content_gaps:
                st.markdown("---")
                st.subheader("Keywords & Content Gaps")
                if kw_suggestions:
                    with st.expander(f"Keyword suggestions ({len(kw_suggestions)})"):
                        kw_rows = [
                            {
                                "Keyword": k.get("keyword", ""),
                                "Intent": k.get("intent", ""),
                                "Type": k.get("recommendation", ""),
                                "Volume": k.get("monthly_volume") or "—",
                                "Difficulty": k.get("difficulty") or "—",
                            }
                            for k in kw_suggestions
                        ]
                        st.dataframe(pd.DataFrame(kw_rows), use_container_width=True)
                if content_gaps:
                    with st.expander(f"Content gaps — missing topics ({len(content_gaps)})"):
                        for gap in content_gaps:
                            st.markdown(
                                f"**{gap.get('suggested_title', gap.get('topic', '?'))}**  \n"
                                f"Keyword: `{gap.get('target_keyword', '—')}` &nbsp; "
                                f"Est. searches: {gap.get('estimated_monthly_searches', '—')}  \n"
                                f"{gap.get('rationale', '')}"
                            )
                            st.markdown("---")

            # ── Content suggestions ───────────────────────────────────────────
            content_suggestions = result.get("content_suggestions") or []
            if content_suggestions:
                st.markdown("---")
                st.subheader(f"Content Suggestions ({len(content_suggestions)})")
                for cs in content_suggestions:
                    stype = cs.get("suggestion_type", "")
                    field = cs.get("field_name", "")
                    risk = cs.get("risk_level", "")
                    risk_color = {"low": "#2ecc71", "medium": "#f39c12", "high": "#e74c3c"}.get(risk, "#888")
                    with st.expander(
                        f"{stype} — {cs.get('page_url', '')[:60]}  "
                        f"[risk: {risk}]"
                    ):
                        st.markdown(
                            f'**Field:** `{field}` &nbsp; '
                            f'<span style="color:{risk_color}">risk: {risk}</span> &nbsp; '
                            f'impact: {cs.get("impact_score", 0):.0f}',
                            unsafe_allow_html=True,
                        )
                        old = cs.get("old_value") or ""
                        new = cs.get("new_value") or ""
                        if old:
                            st.markdown(f"**Before:** {old[:400]}")
                        if new:
                            st.markdown(f"**After:** {new[:400]}")
                        rationale = cs.get("rationale", "")
                        if rationale:
                            st.caption(f"Rationale: {rationale}")

            # ── Execution tasks ───────────────────────────────────────────────
            exec_tasks = result.get("execution_tasks") or []
            if exec_tasks:
                st.markdown("---")
                st.subheader(f"Execution Tasks ({len(exec_tasks)})")
                task_rows = [
                    {
                        "Page": t.get("page_url", "")[:60],
                        "Type": t.get("change_type", ""),
                        "Field": t.get("field_name", ""),
                        "Risk": t.get("risk_level", ""),
                        "Applied": "✅" if t.get("applied") else "—",
                        "Error": t.get("error") or "",
                    }
                    for t in exec_tasks
                ]
                st.dataframe(pd.DataFrame(task_rows), use_container_width=True)
                with st.expander("Task details (before / after)"):
                    for t in exec_tasks:
                        st.markdown(f"**{t.get('change_type','')}** — `{t.get('page_url','')}`")
                        old = t.get("old_value") or ""
                        new = t.get("new_value") or ""
                        if old:
                            st.markdown(f"Before: {str(old)[:300]}")
                        if new:
                            st.markdown(f"After: {str(new)[:300]}")
                        st.markdown("---")

            # ── Validation results ────────────────────────────────────────────
            validation = result.get("validation_results") or []
            if validation:
                st.markdown("---")
                with st.expander(f"Validation results ({len(validation)})"):
                    st.json(validation)

            # ── Ranking data ──────────────────────────────────────────────────
            rankings = result.get("ranking_data") or []
            if rankings:
                st.markdown("---")
                st.subheader(f"Ranking Data ({len(rankings)})")
                rank_rows = [
                    {
                        "Page": r.get("page_url", "")[:60],
                        "Keyword": r.get("keyword", ""),
                        "Position": r.get("position") or "—",
                        "Impressions": r.get("impressions") or "—",
                        "Clicks": r.get("clicks") or "—",
                        "CTR": f"{r['ctr']:.1%}" if r.get("ctr") is not None else "—",
                    }
                    for r in rankings
                ]
                st.dataframe(pd.DataFrame(rank_rows), use_container_width=True)

            # ── Core Web Vitals ───────────────────────────────────────────────
            cwv = result.get("core_web_vitals") or {}
            if cwv and cwv.get("pages_tested"):
                st.markdown("---")
                st.subheader("Core Web Vitals (PageSpeed Insights)")
                st.caption(f"Averaged across {cwv['pages_tested']} pages tested via Google PSI API")

                lh1, lh2, lh3 = st.columns(3)
                with lh1:
                    v = cwv.get("avg_performance_score")
                    color = "normal" if v and v >= 90 else ("off" if v and v >= 50 else "inverse")
                    st.metric("Avg Performance", f"{v}/100" if v is not None else "—",
                              delta="Good" if v and v >= 90 else ("Needs work" if v and v >= 50 else "Poor"),
                              delta_color=color)
                with lh2:
                    v = cwv.get("avg_accessibility_score")
                    color = "normal" if v and v >= 90 else ("off" if v and v >= 70 else "inverse")
                    st.metric("Avg Accessibility", f"{v}/100" if v is not None else "—",
                              delta="Good" if v and v >= 90 else ("Fair" if v and v >= 70 else "Poor"),
                              delta_color=color)
                with lh3:
                    v = cwv.get("avg_seo_score")
                    color = "normal" if v and v >= 90 else ("off" if v and v >= 80 else "inverse")
                    st.metric("Avg Lighthouse SEO", f"{v}/100" if v is not None else "—",
                              delta="Good" if v and v >= 90 else ("Fair" if v and v >= 80 else "Poor"),
                              delta_color=color)

                cw1, cw2, cw3, cw4 = st.columns(4)
                with cw1:
                    lcp = cwv.get("avg_lcp_ms")
                    lcp_s = f"{lcp/1000:.2f}s" if lcp else "—"
                    lcp_status = "✅ Good" if lcp and lcp <= 2500 else ("⚠️ Needs work" if lcp and lcp <= 4000 else "❌ Poor")
                    st.metric("Avg LCP", lcp_s, delta=lcp_status, delta_color="off")
                with cw2:
                    cls = cwv.get("avg_cls")
                    cls_str = f"{cls:.3f}" if cls is not None else "—"
                    cls_status = "✅ Good" if cls is not None and cls <= 0.1 else ("⚠️ Needs work" if cls is not None and cls <= 0.25 else "❌ Poor")
                    st.metric("Avg CLS", cls_str, delta=cls_status, delta_color="off")
                with cw3:
                    fcp = cwv.get("avg_fcp_ms")
                    fcp_s = f"{fcp/1000:.2f}s" if fcp else "—"
                    fcp_status = "✅ Good" if fcp and fcp <= 1800 else ("⚠️ Needs work" if fcp and fcp <= 3000 else "❌ Poor")
                    st.metric("Avg FCP", fcp_s, delta=fcp_status, delta_color="off")
                with cw4:
                    ttfb = cwv.get("avg_ttfb_ms")
                    ttfb_s = f"{ttfb}ms" if ttfb else "—"
                    ttfb_status = "✅ Good" if ttfb and ttfb <= 600 else ("⚠️ Slow" if ttfb and ttfb <= 1800 else "❌ Very slow")
                    st.metric("Avg TTFB", ttfb_s, delta=ttfb_status, delta_color="off")

            # ── Performance report ────────────────────────────────────────────
            perf = result.get("performance_report") or {}
            if perf:
                st.markdown("---")
                st.subheader("Performance Report")
                _render_performance_report(perf)

            with st.expander("Full result JSON"):
                st.json(result)

    # ── Auto-refresh ──────────────────────────────────────────────────────────
    if st.session_state.auto_refresh and status in ("pending", "running"):
        time.sleep(5)
        st.rerun()


# ── Tab: Approval ─────────────────────────────────────────────────────────────

def render_approval() -> None:
    st.header("Approval")

    # Find runs waiting for approval
    all_runs = _get("/api/v1/runs", status="waiting_for_approval", limit=10) or []

    if not all_runs:
        st.info("No runs are currently waiting for approval.")
        return

    run_options = {
        f"{r['run_id'][:12]}… | {r['target_url']} | {r['started_at'][:16].replace('T',' ')}": r["run_id"]
        for r in all_runs
    }
    selected_label = st.selectbox("Select run to review", list(run_options.keys()))
    run_id = run_options[selected_label]

    data = _get(f"/api/v1/pipeline/{run_id}")
    if not data:
        return

    approval_req = data.get("approval_request", {})
    plan = approval_req.get("plan_summary", {})
    tasks = plan.get("tasks", [])
    tasks_count = approval_req.get("tasks_count", len(tasks))
    expires_at = approval_req.get("expires_at", "")
    if expires_at:
        expires_at = expires_at[:16].replace("T", " ")

    # Summary bar
    risk_breakdown = plan.get("risk_breakdown", {})
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Proposed Changes", tasks_count)
    col2.metric("High Risk", risk_breakdown.get("high", 0))
    col3.metric("Medium Risk", risk_breakdown.get("medium", 0))
    col4.metric("Low Risk", risk_breakdown.get("low", 0))

    if expires_at:
        st.caption(f"Approval expires: **{expires_at} UTC**")

    st.markdown("---")

    # Change diff cards
    if tasks:
        st.subheader("Proposed Changes")
        for i, task in enumerate(tasks):
            risk = task.get("risk_level", "low")
            change_type = task.get("change_type", "change").replace("_", " ").title()
            page_url = task.get("page_url") or task.get("url", "—")
            old_val = task.get("old_value", "") or ""
            new_val = task.get("new_value", "") or task.get("proposed_value", "") or ""

            with st.expander(
                f"{_risk_badge(risk)} &nbsp; {change_type} — `{page_url}`",
                expanded=(i < 3),
            ):
                diff_col1, diff_col2 = st.columns(2)
                with diff_col1:
                    st.markdown("**Current Value**")
                    st.code(old_val or "*(empty)*", language=None)
                with diff_col2:
                    st.markdown("**Proposed Value**")
                    st.code(new_val or "*(empty)*", language=None)

                if task.get("rationale"):
                    st.caption(f"Rationale: {task['rationale']}")
    else:
        st.info("No individual task details available in plan summary.")
        with st.expander("Raw plan data"):
            st.json(plan)

    st.markdown("---")

    # Approve / Reject controls
    st.subheader("Decision")
    reviewer = st.text_input("Your name / identifier", value="ui_user")
    notes = st.text_area("Notes (optional)", height=80)

    col_approve, col_reject, _ = st.columns([1, 1, 2])
    with col_approve:
        if st.button("Approve", type="primary", use_container_width=True):
            result = _post(
                f"/api/v1/pipeline/{run_id}/approve",
                {"reviewer": reviewer, "notes": notes},
            )
            if result:
                queue_status = result.get("queue_status", "unknown")
                if queue_status == "enqueued":
                    st.success("Approved! Pipeline has been queued to resume.")
                elif queue_status == "in_process_fallback":
                    st.warning(
                        "Approved. Celery is unavailable, so resume is running inside the API process."
                    )
                else:
                    st.warning(
                        f"Approved, but queue status is `{queue_status}`. "
                        "If it does not resume, verify Redis + Celery worker."
                    )
                _set_current_run(run_id)
                time.sleep(1)
                st.rerun()

    with col_reject:
        if st.button("Reject", type="secondary", use_container_width=True):
            result = _post(
                f"/api/v1/pipeline/{run_id}/reject",
                {"reviewer": reviewer, "notes": notes},
            )
            if result:
                st.warning("Rejected. Pipeline has been cancelled.")
                time.sleep(1)
                st.rerun()


# ── Tab: History ──────────────────────────────────────────────────────────────

def render_history() -> None:
    st.header("Run History")

    col1, col2 = st.columns([2, 1])
    with col1:
        status_filter = st.selectbox(
            "Filter by status",
            ["All", "completed", "failed", "running", "waiting_for_approval", "cancelled", "rejected"],
        )
    with col2:
        limit = st.number_input("Max rows", min_value=5, max_value=100, value=20, step=5)

    params: dict = {"limit": limit}
    if status_filter != "All":
        params["status"] = status_filter

    runs = _get("/api/v1/runs", **params) or []

    if not runs:
        st.info("No runs found.")
        return

    st.markdown(f"Showing **{len(runs)}** run(s)")
    st.markdown("---")

    for run in runs:
        status = run.get("status", "")
        mode = run.get("mode", "")
        started = run.get("started_at", "")[:16].replace("T", " ")
        ended = run.get("ended_at") or ""
        ended_str = ended[:16].replace("T", " ") if ended else "—"
        run_id = run.get("run_id", "")
        target = run.get("target_url", "")

        col_status, col_info, col_action = st.columns([2, 4, 1])
        with col_status:
            st.markdown(_status_badge(status), unsafe_allow_html=True)
            st.caption(mode)
        with col_info:
            st.markdown(f"**`{run_id[:12]}…`** — {target}")
            st.caption(f"Started: {started} &nbsp;|&nbsp; Ended: {ended_str}")
        with col_action:
            if st.button("Load", key=f"load_{run_id}"):
                _set_current_run(run_id)
                st.success(f"Loaded run `{run_id[:12]}…`. Check the Current Run tab.")

        st.markdown('<hr style="margin:6px 0;border-color:#eee">', unsafe_allow_html=True)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    _init_state()
    _rehydrate_current_run()
    mode, target_url, site_id = render_sidebar()

    # Pending approval badge in tab title
    pending_runs = _get("/api/v1/runs", status="waiting_for_approval", limit=5) or []
    pending_count = len(pending_runs)
    approval_tab_label = f"Approval ({pending_count})" if pending_count else "Approval"

    tab_dashboard, tab_run, tab_approval, tab_history = st.tabs(
        ["Dashboard", "Current Run", approval_tab_label, "History"]
    )

    with tab_dashboard:
        render_dashboard()

    with tab_run:
        render_current_run()

    with tab_approval:
        render_approval()

    with tab_history:
        render_history()


if __name__ == "__main__":
    main()
