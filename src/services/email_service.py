"""Email notification service — sends approval alerts to multiple recipients.

Uses aiosmtplib for async SMTP (no blocking the Celery event loop).
Sends HTML emails with a full before/after table of proposed SEO changes.

Configuration (all via .env or config/default.toml):
    SEO_AGENT_EMAIL_ENABLED=true
    SEO_AGENT_EMAIL_SMTP_HOST=smtp.gmail.com
    SEO_AGENT_EMAIL_SMTP_PORT=587
    SEO_AGENT_EMAIL_SMTP_USER=you@gmail.com
    SEO_AGENT_EMAIL_SMTP_PASSWORD=app-password
    SEO_AGENT_EMAIL_RECIPIENTS=alice@co.com,bob@co.com,carol@co.com
    SEO_AGENT_EMAIL_APP_BASE_URL=https://your-dashboard.com
    SEO_AGENT_STREAMLIT_URL=http://localhost:8501
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

import structlog

from src.config import get_settings

settings = get_settings()
logger = structlog.get_logger(__name__)


# ── HTML template helpers ─────────────────────────────────────────────────────

_CHANGE_TYPE_LABELS = {
    "meta_title":       "SEO Title",
    "meta_description": "Meta Description",
    "focus_keyword":    "Focus Keyword",
    "content_rewrite":  "Content Rewrite",
    "new_section":      "New Section",
    "schema":           "Schema Markup",
    "new_page":         "New Page",
}

_RISK_COLOURS = {
    "low":    "#2ecc71",
    "medium": "#f39c12",
    "high":   "#e74c3c",
}


def _risk_badge(level: str) -> str:
    colour = _RISK_COLOURS.get(level, "#95a5a6")
    return (
        f'<span style="background:{colour};color:#fff;padding:2px 8px;'
        f'border-radius:3px;font-size:11px;font-weight:bold;">'
        f'{level.upper()}</span>'
    )


def _truncate(value: str | None, max_len: int = 120) -> str:
    if not value:
        return '<em style="color:#aaa">—</em>'
    value = str(value)
    if len(value) > max_len:
        return value[:max_len] + "…"
    return value


def _build_html(
    run_id: str,
    target_url: str,
    tasks: list[dict[str, Any]],
    tasks_count: int,
    risk_breakdown: dict[str, int],
    expires_at: str,
    review_url: str,
) -> str:
    # ── Changes table rows ────────────────────────────────────────────────────
    rows = ""
    for i, task in enumerate(tasks):
        change_label = _CHANGE_TYPE_LABELS.get(task.get("change_type", ""), task.get("change_type", ""))
        bg = "#f9f9f9" if i % 2 == 0 else "#ffffff"
        rows += f"""
        <tr style="background:{bg}">
            <td style="padding:8px 12px;border-bottom:1px solid #eee;">
                <a href="{task.get('page_url','')}" style="color:#3498db;font-size:12px;">
                    {task.get('page_url','—')}
                </a>
            </td>
            <td style="padding:8px 12px;border-bottom:1px solid #eee;font-size:12px;">{change_label}</td>
            <td style="padding:8px 12px;border-bottom:1px solid #eee;font-size:12px;color:#e74c3c;">
                {_truncate(task.get('old_value'))}
            </td>
            <td style="padding:8px 12px;border-bottom:1px solid #eee;font-size:12px;color:#27ae60;">
                {_truncate(task.get('new_value'))}
            </td>
            <td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:center;">
                {_risk_badge(task.get('risk_level','low'))}
            </td>
        </tr>"""

    if not rows:
        rows = '<tr><td colspan="5" style="padding:16px;text-align:center;color:#aaa;">No task details available</td></tr>'

    # ── Risk summary badges ────────────────────────────────────────────────────
    risk_summary = " &nbsp; ".join(
        f'{_risk_badge(lvl)} <span style="font-size:12px;">{cnt}</span>'
        for lvl, cnt in risk_breakdown.items()
        if cnt > 0
    )

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;font-family:Arial,sans-serif;background:#f4f6f8;">

<table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f6f8;padding:30px 0;">
  <tr><td align="center">
  <table width="640" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.08);">

    <!-- Header -->
    <tr style="background:#2c3e50;">
      <td style="padding:24px 32px;">
        <p style="margin:0;color:#ecf0f1;font-size:11px;letter-spacing:1px;text-transform:uppercase;">Automated SEO Agent</p>
        <h1 style="margin:8px 0 0;color:#fff;font-size:22px;">Changes awaiting your approval</h1>
      </td>
    </tr>

    <!-- Site + meta -->
    <tr><td style="padding:24px 32px 0;">
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td style="width:50%;padding-right:12px;">
            <p style="margin:0;font-size:11px;color:#7f8c8d;text-transform:uppercase;letter-spacing:.5px;">Site</p>
            <p style="margin:4px 0 0;font-size:14px;color:#2c3e50;word-break:break-all;">{target_url}</p>
          </td>
          <td style="width:25%;padding-right:12px;">
            <p style="margin:0;font-size:11px;color:#7f8c8d;text-transform:uppercase;letter-spacing:.5px;">Changes</p>
            <p style="margin:4px 0 0;font-size:22px;font-weight:bold;color:#2c3e50;">{tasks_count}</p>
          </td>
          <td style="width:25%;">
            <p style="margin:0;font-size:11px;color:#7f8c8d;text-transform:uppercase;letter-spacing:.5px;">Expires</p>
            <p style="margin:4px 0 0;font-size:13px;color:#e74c3c;">{expires_at}</p>
          </td>
        </tr>
      </table>
      <p style="margin:16px 0 0;font-size:13px;">Risk breakdown: &nbsp; {risk_summary or '—'}</p>
    </td></tr>

    <!-- Changes table -->
    <tr><td style="padding:20px 32px;">
      <h3 style="margin:0 0 12px;font-size:14px;color:#2c3e50;">Proposed Changes</h3>
      <table width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #eee;border-radius:4px;border-collapse:collapse;">
        <thead style="background:#ecf0f1;">
          <tr>
            <th style="padding:8px 12px;text-align:left;font-size:11px;color:#7f8c8d;font-weight:600;">Page</th>
            <th style="padding:8px 12px;text-align:left;font-size:11px;color:#7f8c8d;font-weight:600;">Type</th>
            <th style="padding:8px 12px;text-align:left;font-size:11px;color:#7f8c8d;font-weight:600;">Current</th>
            <th style="padding:8px 12px;text-align:left;font-size:11px;color:#7f8c8d;font-weight:600;">Proposed</th>
            <th style="padding:8px 12px;text-align:center;font-size:11px;color:#7f8c8d;font-weight:600;">Risk</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
    </td></tr>

    <!-- Review button -->
    <tr><td style="padding:8px 32px 28px;">
      <a href="{review_url}"
         style="display:inline-block;background:#2c3e50;color:#fff;padding:14px 32px;
                border-radius:5px;text-decoration:none;font-size:15px;font-weight:bold;">
        👁 Review &amp; Approve Changes
      </a>
      <p style="margin:16px 0 0;font-size:12px;color:#7f8c8d;">
        Opens the SEO Agent dashboard in your browser. Go to the <strong>Approval</strong> tab
        to review every proposed change and approve or reject.
      </p>
    </td></tr>

    <!-- Footer -->
    <tr style="background:#f4f6f8;">
      <td style="padding:16px 32px;">
        <p style="margin:0;font-size:11px;color:#aaa;">
          Run ID: {run_id}
        </p>
      </td>
    </tr>

  </table>
  </td></tr>
</table>
</body>
</html>"""


def _build_plain_text(
    run_id: str,
    target_url: str,
    tasks: list[dict[str, Any]],
    tasks_count: int,
    expires_at: str,
    review_url: str,
) -> str:
    lines = [
        "SEO AGENT — APPROVAL REQUIRED",
        "=" * 50,
        f"Site:     {target_url}",
        f"Changes:  {tasks_count}",
        f"Expires:  {expires_at}",
        f"Run ID:   {run_id}",
        "",
        "PROPOSED CHANGES",
        "-" * 50,
    ]
    for task in tasks:
        change_label = _CHANGE_TYPE_LABELS.get(task.get("change_type", ""), task.get("change_type", ""))
        lines += [
            f"Page:     {task.get('page_url', '—')}",
            f"Type:     {change_label}  [{task.get('risk_level','low').upper()} risk]",
            f"Current:  {str(task.get('old_value') or '(empty)')[:100]}",
            f"Proposed: {str(task.get('new_value') or '')[:100]}",
            "",
        ]
    lines += [
        "ACTIONS",
        "-" * 50,
        f"Open the SEO Agent dashboard to review and approve: {review_url}",
        "(Go to the Approval tab)",
    ]
    return "\n".join(lines)


# ── Public send function ──────────────────────────────────────────────────────

async def send_approval_email(
    run_id: str,
    target_url: str,
    plan_summary: dict[str, Any],
    expires_at: datetime | None = None,
) -> bool:
    """Send approval notification email to all configured recipients.

    Args:
        run_id:       WorkflowRun.run_id (used to build API links)
        target_url:   The WordPress site URL being optimised
        plan_summary: The plan_summary dict from ApprovalRequest
        expires_at:   When the approval window closes

    Returns:
        True if email sent to at least one recipient, False otherwise.
    """
    if not settings.email_enabled:
        logger.debug("email_service.disabled")
        return False

    recipients = settings.email_recipients
    if not recipients:
        logger.warning("email_service.no_recipients")
        return False

    if not settings.email_smtp_user or not settings.email_smtp_password:
        logger.warning("email_service.no_smtp_credentials")
        return False

    tasks: list[dict[str, Any]] = plan_summary.get("tasks", [])
    tasks_count: int = plan_summary.get("total_tasks", len(tasks))
    risk_breakdown: dict[str, int] = plan_summary.get("risk_breakdown", {})

    review_url = settings.email_streamlit_url.rstrip("/")

    expires_str = expires_at.strftime("%Y-%m-%d %H:%M UTC") if expires_at else "72 hours from now"

    subject = f"[SEO Agent] {tasks_count} change(s) need approval — {target_url}"

    html_body = _build_html(
        run_id=run_id,
        target_url=target_url,
        tasks=tasks,
        tasks_count=tasks_count,
        risk_breakdown=risk_breakdown,
        expires_at=expires_str,
        review_url=review_url,
    )
    plain_body = _build_plain_text(
        run_id=run_id,
        target_url=target_url,
        tasks=tasks,
        tasks_count=tasks_count,
        expires_at=expires_str,
        review_url=review_url,
    )

    # Send individually so one bad address doesn't block others
    sent = 0
    for recipient in recipients:
        try:
            await _send_one(
                to=recipient,
                subject=subject,
                html_body=html_body,
                plain_body=plain_body,
            )
            sent += 1
            logger.info("email_service.sent", run_id=run_id, to=recipient)
        except Exception as exc:
            logger.error("email_service.send_failed", run_id=run_id, to=recipient, error=str(exc))

    return sent > 0


async def _send_one(to: str, subject: str, html_body: str, plain_body: str) -> None:
    """Send a single email via SMTP with TLS."""
    import aiosmtplib

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{settings.email_from_name} <{settings.email_from_address}>"
    msg["To"] = to

    msg.attach(MIMEText(plain_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    await aiosmtplib.send(
        msg,
        hostname=settings.email_smtp_host,
        port=settings.email_smtp_port,
        username=settings.email_smtp_user,
        password=settings.email_smtp_password,
        start_tls=True,
    )
