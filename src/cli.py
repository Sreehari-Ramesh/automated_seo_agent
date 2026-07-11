"""CLI entry point — seo-agent command."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import structlog
import typer
import uvicorn
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from src.config import get_settings

settings = get_settings()
console = Console()
app = typer.Typer(
    name="seo-agent",
    help="Automated SEO Agent — WordPress SEO automation powered by Claude + LangGraph",
    no_args_is_help=True,
)

# ── Logging setup ─────────────────────────────────────────────────────────────

import logging
import structlog

structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ],
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    stream=sys.stdout,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _check_env():
    """Warn if critical env vars are missing."""
    warnings = []
    if not settings.anthropic_api_key:
        warnings.append("[red]ANTHROPIC_API_KEY[/red] not set — AI agents will fail")
    if not settings.wp_base_url:
        warnings.append("[yellow]WP_BASE_URL[/yellow] not set — WordPress operations disabled")
    if warnings:
        for w in warnings:
            console.print(f"  [bold yellow]⚠ Warning:[/bold yellow] {w}")


def _print_issues_table(issues: list[dict]):
    table = Table(title=f"SEO Issues ({len(issues)} total)", show_lines=True)
    table.add_column("Severity", style="bold", width=10)
    table.add_column("Category", width=12)
    table.add_column("Issue", width=40)
    table.add_column("Page", width=40)
    table.add_column("Impact", width=8)

    severity_styles = {
        "critical": "red",
        "high": "yellow",
        "medium": "blue",
        "low": "dim",
    }

    for issue in issues[:30]:
        sev = issue.get("severity", "low")
        style = severity_styles.get(sev, "")
        table.add_row(
            f"[{style}]{sev.upper()}[/{style}]",
            issue.get("category", ""),
            issue.get("title", ""),
            issue.get("page_url", "")[-40:],
            str(int(issue.get("impact_score", 0))),
        )
    console.print(table)


# ── Commands ───────────────────────────────────────────────────────────────────

@app.command()
def scan(
    url: str = typer.Argument(
        default=None, help="Target WordPress URL (overrides WP_BASE_URL env var)"
    ),
    output: str = typer.Option(None, "--output", "-o", help="Save results to JSON file"),
):
    """Crawl + audit + keyword planning (read-only, no changes applied)."""
    target = url or settings.wp_base_url
    if not target:
        console.print("[red]Error:[/red] No target URL. Set WP_BASE_URL or pass as argument.")
        raise typer.Exit(1)

    _check_env()
    console.print(Panel(f"[bold]SEO Scan[/bold]\n{target}", border_style="blue"))

    async def _run():
        from src.graph.seo_graph import run_seo_pipeline

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task = progress.add_task("Running SEO pipeline...", total=None)
            result = await run_seo_pipeline(
                target_url=target,
                mode="dry_run",
                approval_status="pending",
            )
            progress.update(task, description="Complete!")

        return result

    result = asyncio.run(_run())

    # Summary
    console.print(f"\n[bold green]Scan Complete[/bold green]")
    console.print(f"  Pages crawled: [bold]{len(result.get('crawled_pages', []))}[/bold]")
    console.print(f"  SEO score: [bold]{result.get('overall_seo_score', 0)}/100[/bold]")
    console.print(f"  Issues found: [bold]{len(result.get('issues', []))}[/bold]")

    issues = result.get("issues", [])
    if issues:
        _print_issues_table(issues)

    if result.get("content_gaps"):
        console.print(f"\n[bold]Content Gap Opportunities:[/bold] {len(result['content_gaps'])}")
        for gap in result["content_gaps"][:3]:
            console.print(f"  • {gap.get('suggested_title', gap.get('topic', ''))}")

    if output:
        Path(output).write_text(json.dumps(result, indent=2, default=str))
        console.print(f"\n[dim]Results saved to {output}[/dim]")


@app.command()
def execute(
    url: str = typer.Argument(default=None, help="Target WordPress URL"),
    mode: str = typer.Option(
        "dry_run",
        "--mode", "-m",
        help="Execution mode: dry_run | preview | live",
    ),
    auto_approve: bool = typer.Option(
        False,
        "--auto-approve",
        help="Skip human approval (use with caution in live mode)",
    ),
):
    """Apply SEO improvements to WordPress (requires --mode live for real changes)."""
    target = url or settings.wp_base_url
    if not target:
        console.print("[red]Error:[/red] No target URL.")
        raise typer.Exit(1)

    _check_env()

    if mode == "live" and not auto_approve:
        confirmed = typer.confirm(
            f"⚠️  You are about to apply LIVE changes to {target}. Continue?"
        )
        if not confirmed:
            raise typer.Exit(0)

    console.print(Panel(
        f"[bold]SEO Execute[/bold] — Mode: [yellow]{mode}[/yellow]\n{target}",
        border_style="yellow" if mode == "live" else "blue",
    ))

    async def _run():
        from src.graph.seo_graph import run_seo_pipeline

        return await run_seo_pipeline(
            target_url=target,
            mode=mode,
            approval_status="approved" if auto_approve else "pending",
        )

    result = asyncio.run(_run())

    applied = result.get("applied_changes", [])
    failed = result.get("failed_changes", [])

    console.print(f"\n[bold green]Execution Complete[/bold green] [{mode} mode]")
    console.print(f"  Applied: [green]{len(applied)}[/green]")
    console.print(f"  Failed: [red]{len(failed)}[/red]")
    console.print(f"  Rolled back: [yellow]{len(result.get('rollback_triggered', []))}[/yellow]")

    if failed:
        console.print("\n[bold red]Failed changes:[/bold red]")
        for t in failed[:5]:
            console.print(f"  • {t.get('change_type')} on {t.get('page_url')}: {t.get('error')}")


@app.command()
def pipeline(
    url: str = typer.Argument(default=None, help="Target WordPress URL"),
    mode: str = typer.Option("dry_run", "--mode", "-m"),
    auto_approve: bool = typer.Option(False, "--auto-approve"),
    output: str = typer.Option(None, "--output", "-o", help="Save full report to JSON"),
):
    """Run the complete SEO pipeline: scan + plan + execute + validate + report."""
    target = url or settings.wp_base_url
    if not target:
        console.print("[red]Error:[/red] No target URL.")
        raise typer.Exit(1)

    _check_env()
    console.print(Panel(
        f"[bold]Full SEO Pipeline[/bold] — Mode: [yellow]{mode}[/yellow]\n{target}",
        border_style="green",
    ))

    async def _run():
        from src.graph.seo_graph import run_seo_pipeline

        return await run_seo_pipeline(
            target_url=target,
            mode=mode,
            approval_status="approved" if auto_approve else "pending",
        )

    result = asyncio.run(_run())

    report = result.get("performance_report", {})
    if report.get("ai_report"):
        console.print(Panel(
            report["ai_report"],
            title="[bold]AI Performance Report[/bold]",
            border_style="green",
        ))

    if output:
        Path(output).write_text(json.dumps(result, indent=2, default=str))
        console.print(f"\n[dim]Full report saved to {output}[/dim]")


@app.command()
def serve(
    host: str = typer.Option(settings.api_host, "--host", "-h"),
    port: int = typer.Option(settings.api_port, "--port", "-p"),
    with_scheduler: bool = typer.Option(True, "--scheduler/--no-scheduler"),
    reload: bool = typer.Option(False, "--reload"),
):
    """Start the FastAPI server (+ optional background scheduler)."""
    _check_env()
    console.print(Panel(
        f"[bold]SEO Agent API Server[/bold]\n"
        f"http://{host}:{port}\n"
        f"Docs: http://{host}:{port}/docs\n"
        f"Scheduler: {'enabled' if with_scheduler else 'disabled'}",
        border_style="green",
    ))

    from src.api.routes import create_app
    app_instance = create_app()

    if with_scheduler:
        from src.scheduler.scheduler import start_scheduler, stop_scheduler

        @app_instance.on_event("startup")
        async def _startup_scheduler() -> None:
            start_scheduler()

        @app_instance.on_event("shutdown")
        async def _shutdown_scheduler() -> None:
            stop_scheduler()

    uvicorn.run(app_instance, host=host, port=port, reload=reload)


@app.command("show-config")
def show_config():
    """Display the current configuration."""
    table = Table(title="Active Configuration", show_lines=True)
    table.add_column("Setting", style="cyan")
    table.add_column("Value")

    entries = [
        ("Claude Model", settings.claude_model),
        ("Execution Mode", settings.execution_mode),
        ("WP Base URL", settings.wp_base_url or "[dim]not set[/dim]"),
        ("WP Username", settings.wp_username or "[dim]not set[/dim]"),
        ("Anthropic API Key", "***" if settings.anthropic_api_key else "[red]NOT SET[/red]"),
        ("Database URL", settings.database_url),
        ("Max Pages", str(settings.wp_max_pages)),
        ("Auto Execute Threshold", f"{settings.auto_execute_threshold}/100"),
        ("Max Batch Size", str(settings.max_batch_size)),
        ("Min Change Interval", f"{settings.min_change_interval_days} days"),
        ("Pipeline Cron", settings.pipeline_cron),
        ("Execution Cron", settings.execution_cron),
        ("Log Level", settings.log_level),
        ("Environment", settings.env),
    ]
    for k, v in entries:
        table.add_row(k, str(v))
    console.print(table)


if __name__ == "__main__":
    app()
