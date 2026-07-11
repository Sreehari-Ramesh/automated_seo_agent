"""Application settings — loaded from src/config/default.toml + environment variables."""

from __future__ import annotations

import tomli
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse, urlunparse

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEFAULT_CONFIG = Path(__file__).with_name("default.toml")


def _load_deployment_config() -> dict:
    try:
        from src.config.deployment_config import DOCKER_ENV  # type: ignore
        if isinstance(DOCKER_ENV, dict):
            return DOCKER_ENV
    except Exception:
        pass
    return {}


_DEPLOY = _load_deployment_config()


def _deploy(key: str, fallback):
    value = _DEPLOY.get(key)
    if value is None:
        return fallback
    if isinstance(value, str) and not value.strip():
        return fallback
    return value


def _is_docker_runtime() -> bool:
    return Path("/.dockerenv").exists()


def _normalize_redis_url(url: str) -> str:
    """Use localhost Redis automatically when running outside Docker."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("redis", "rediss"):
            return url
        if _is_docker_runtime():
            return url
        if parsed.hostname != "redis":
            return url
        host = "127.0.0.1"
        netloc = host
        if parsed.port:
            netloc = f"{host}:{parsed.port}"
        if parsed.username:
            auth = parsed.username
            if parsed.password:
                auth += f":{parsed.password}"
            netloc = f"{auth}@{netloc}"
        return urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))
    except Exception:
        return url


def _load_toml() -> dict:
    if _DEFAULT_CONFIG.exists():
        with open(_DEFAULT_CONFIG, "rb") as f:
            return tomli.load(f)
    return {}


_TOML = _load_toml()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SEO_AGENT_",
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        # Config-only mode: ignore OS env and .env overrides.
        return (init_settings,)

    # ── App ──────────────────────────────────────────────────────────────────
    env: Literal["development", "production", "test"] = Field(
        default_factory=lambda: _deploy("SEO_AGENT_ENV", _TOML.get("app", {}).get("env", "development"))
    )
    log_level: str = Field(default_factory=lambda: _deploy("LOG_LEVEL", _TOML.get("app", {}).get("log_level", "INFO")))
    data_dir: Path = Field(
        default_factory=lambda: Path(_TOML.get("app", {}).get("data_dir", "./data"))
    )

    # ── Claude ───────────────────────────────────────────────────────────────
    anthropic_api_key: str = Field(default_factory=lambda: _deploy("ANTHROPIC_API_KEY", ""), alias="ANTHROPIC_API_KEY")
    claude_model: str = Field(
        default_factory=lambda: _TOML.get("claude", {}).get("model", "claude-sonnet-4-6")
    )
    claude_temperature: float = Field(
        default_factory=lambda: _TOML.get("claude", {}).get("temperature", 0.0)
    )
    claude_max_tokens: int = Field(
        default_factory=lambda: _TOML.get("claude", {}).get("max_tokens", 4096)
    )

    # ── Openai ───────────────────────────────────────────────────────────────
    openai_api_key: str = Field(default_factory=lambda: _deploy("OPENAI_API_KEY", ""), alias="OPENAI_API_KEY")
    openai_model: str = Field(
        default_factory=lambda: _TOML.get("openai", {}).get("model", "gpt-4.1-2025-04-14")
    )
    openai_temperature: float = Field(
        default_factory=lambda: _TOML.get("openai", {}).get("temperature", 0.2)
    )
    openai_max_tokens: int = Field(
        default_factory=lambda: _TOML.get("openai", {}).get("max_tokens", 4096)
    )
    # ── WordPress ────────────────────────────────────────────────────────────
    wp_base_url: str = Field(default_factory=lambda: _deploy("WP_BASE_URL", ""), alias="WP_BASE_URL")
    wp_username: str = Field(default_factory=lambda: _deploy("WP_USERNAME", ""), alias="WP_USERNAME")
    wp_app_password: str = Field(default_factory=lambda: _deploy("WP_APP_PASSWORD", ""), alias="WP_APP_PASSWORD")
    wp_request_timeout: int = Field(
        default_factory=lambda: _TOML.get("wordpress", {}).get("request_timeout", 30)
    )
    wp_max_pages: int = Field(
        default_factory=lambda: int(_deploy("SEO_AGENT_WP_MAX_PAGES", _TOML.get("wordpress", {}).get("max_pages", 200)))
    )
    # print(f"Loaded WordPress settings: base_url={wp_base_url}, username={wp_username}, timeout={wp_request_timeout}s, max_pages={wp_max_pages}")

    # ── Database ─────────────────────────────────────────────────────────────
    # Postgres (production)
    postgres_url: str = Field(default_factory=lambda: _deploy("POSTGRES_URL", ""), alias="POSTGRES_URL")
    postgres_sync_url: str = Field(default_factory=lambda: _deploy("POSTGRES_SYNC_URL", ""), alias="POSTGRES_SYNC_URL")
    # LangGraph checkpoint connection string (plain psycopg3 format)
    langgraph_checkpoint_url: str = Field(default_factory=lambda: _deploy("LANGGRAPH_CHECKPOINT_URL", ""), alias="LANGGRAPH_CHECKPOINT_URL")
    # SQLite fallback (dev)
    database_url: str = Field(default="", alias="DATABASE_URL")

    @model_validator(mode="after")
    def resolve_database_url(self) -> "Settings":
        # Prefer Postgres, fall back to SQLite
        if not self.database_url:
            if self.postgres_url:
                self.database_url = self.postgres_url
            else:
                db_path = self.data_dir / "seo_agent.db"
                self.database_url = f"sqlite+aiosqlite:///{db_path}"
        return self

    @model_validator(mode="after")
    def warn_production_gaps(self) -> "Settings":
        if self.env != "production":
            return self
        import warnings
        if not self.postgres_url:
            warnings.warn(
                "[SEO Agent] Running in production with SQLite — set POSTGRES_URL for a real database.",
                stacklevel=2,
            )
        if not self.api_secret:
            warnings.warn(
                "[SEO Agent] SEO_AGENT_API_SECRET is not set — the API is unauthenticated in production.",
                stacklevel=2,
            )
        if not self.allowed_origins_raw:
            warnings.warn(
                "[SEO Agent] SEO_AGENT_ALLOWED_ORIGINS is not set — CORS allows all origins (*) in production.",
                stacklevel=2,
            )
        localhost_urls = {
            "SEO_AGENT_STREAMLIT_URL": self.email_streamlit_url,
            "SEO_AGENT_EMAIL_APP_BASE_URL": self.email_app_base_url,
        }
        for var, val in localhost_urls.items():
            if "localhost" in val or "127.0.0.1" in val:
                warnings.warn(
                    f"[SEO Agent] {var}={val!r} still points to localhost in production.",
                    stacklevel=2,
                )
        return self

    @property
    def use_postgres(self) -> bool:
        return bool(self.postgres_url)

    # ── Redis ─────────────────────────────────────────────────────────────────
    redis_url: str = Field(
        default_factory=lambda: _normalize_redis_url(
            _deploy("REDIS_URL", _TOML.get("redis", {}).get("url", "redis://localhost:6379/0"))
        ),
        alias="REDIS_URL",
    )

    # ── Celery Worker ─────────────────────────────────────────────────────────
    worker_concurrency: int = Field(
        default_factory=lambda: _TOML.get("worker", {}).get("concurrency", 4)
    )
    worker_soft_time_limit: int = Field(
        default_factory=lambda: _TOML.get("worker", {}).get("soft_time_limit", 3600)
    )
    worker_hard_time_limit: int = Field(
        default_factory=lambda: _TOML.get("worker", {}).get("hard_time_limit", 7200)
    )
    pipeline_queue: str = Field(
        default_factory=lambda: _TOML.get("worker", {}).get("pipeline_queue", "seo_pipeline")
    )
    monitor_queue: str = Field(
        default_factory=lambda: _TOML.get("worker", {}).get("monitor_queue", "seo_monitor")
    )

    # ── Crawler ──────────────────────────────────────────────────────────────
    crawler_concurrency: int = Field(
        default_factory=lambda: _TOML.get("crawler", {}).get("concurrency", 5)
    )
    crawler_delay: float = Field(
        default_factory=lambda: _TOML.get("crawler", {}).get("request_delay", 0.5)
    )
    crawler_respect_robots: bool = Field(
        default_factory=lambda: _TOML.get("crawler", {}).get("respect_robots", True)
    )
    crawler_max_depth: int = Field(
        default_factory=lambda: _TOML.get("crawler", {}).get("max_depth", 5)
    )
    crawler_user_agent: str = Field(
        default_factory=lambda: _TOML.get("crawler", {}).get("user_agent", "SEOAgent/1.0")
    )

    # ── SEO Audit ────────────────────────────────────────────────────────────
    seo_min_word_count: int = Field(
        default_factory=lambda: _TOML.get("seo_audit", {}).get("min_word_count", 300)
    )
    seo_title_min: int = Field(
        default_factory=lambda: _TOML.get("seo_audit", {}).get("title_min_length", 30)
    )
    seo_title_max: int = Field(
        default_factory=lambda: _TOML.get("seo_audit", {}).get("title_max_length", 60)
    )
    seo_desc_min: int = Field(
        default_factory=lambda: _TOML.get("seo_audit", {}).get("meta_desc_min_length", 120)
    )
    seo_desc_max: int = Field(
        default_factory=lambda: _TOML.get("seo_audit", {}).get("meta_desc_max_length", 160)
    )

    # ── Scoring / Execution ──────────────────────────────────────────────────
    auto_execute_threshold: int = Field(
        default_factory=lambda: _TOML.get("scoring", {}).get("auto_execute_threshold", 85)
    )
    min_change_interval_days: int = Field(
        default_factory=lambda: _TOML.get("scoring", {}).get("min_change_interval_days", 7)
    )
    max_batch_size: int = Field(
        default_factory=lambda: _TOML.get("scoring", {}).get("max_batch_size", 10)
    )
    execution_mode: Literal["dry_run", "preview", "live"] = Field(
        default_factory=lambda: _TOML.get("execution", {}).get("mode", "dry_run")
    )
    auto_rollback: bool = Field(
        default_factory=lambda: _TOML.get("execution", {}).get("auto_rollback", True)
    )
    validation_delay_hours: int = Field(
        default_factory=lambda: _TOML.get("execution", {}).get("validation_delay_hours", 48)
    )

    # ── Policy Engine ─────────────────────────────────────────────────────────
    policy_meta_cooldown_days: int = Field(
        default_factory=lambda: _TOML.get("policy", {}).get("meta_cooldown_days", 14)
    )
    policy_content_cooldown_days: int = Field(
        default_factory=lambda: _TOML.get("policy", {}).get("content_cooldown_days", 21)
    )
    policy_new_page_manual_only: bool = Field(
        default_factory=lambda: _TOML.get("policy", {}).get("new_page_manual_only", True)
    )
    policy_homepage_manual_only: bool = Field(
        default_factory=lambda: _TOML.get("policy", {}).get("homepage_manual_only", True)
    )
    policy_low_impression_threshold: int = Field(
        default_factory=lambda: _TOML.get("policy", {}).get("low_impression_threshold", 100)
    )
    policy_max_failed_retries: int = Field(
        default_factory=lambda: _TOML.get("policy", {}).get("max_failed_retries", 3)
    )
    policy_approval_expiry_hours: int = Field(
        default_factory=lambda: _TOML.get("policy", {}).get("approval_expiry_hours", 72)
    )

    # ── API ──────────────────────────────────────────────────────────────────
    api_host: str = Field(default_factory=lambda: _TOML.get("api", {}).get("host", "0.0.0.0"))
    api_port: int = Field(default_factory=lambda: _TOML.get("api", {}).get("port", 8000))
    api_secret: str = Field(default_factory=lambda: _deploy("SEO_AGENT_API_SECRET", ""), alias="SEO_AGENT_API_SECRET")
    # Comma-separated list of allowed CORS origins.
    # In production set to your exact frontend URLs, e.g.:
    #   SEO_AGENT_ALLOWED_ORIGINS=http://your-server:8501,https://yourdomain.com
    # Defaults to ["*"] in development only.
    allowed_origins_raw: str = Field(default_factory=lambda: _deploy("SEO_AGENT_ALLOWED_ORIGINS", ""), alias="SEO_AGENT_ALLOWED_ORIGINS")

    @property
    def allowed_origins(self) -> list[str]:
        if self.allowed_origins_raw.strip():
            return [o.strip() for o in self.allowed_origins_raw.split(",") if o.strip()]
        if self.env == "production":
            import structlog
            structlog.get_logger(__name__).warning(
                "cors.wildcard_in_production",
                hint="Set SEO_AGENT_ALLOWED_ORIGINS to restrict CORS in production",
            )
        return ["*"]

    # ── Google / SERP ────────────────────────────────────────────────────────
    google_pagespeed_api_key: str = Field(default_factory=lambda: _deploy("GOOGLE_PAGESPEED_API_KEY", ""), alias="GOOGLE_PAGESPEED_API_KEY")
    serp_api_key: str = Field(default_factory=lambda: _deploy("SERP_API_KEY", ""), alias="SERP_API_KEY")
    # Comma-separated in env; list in TOML [competitive]
    competitor_urls_raw: str = Field(default_factory=lambda: _deploy("SEO_AGENT_COMPETITOR_URLS", ""), alias="SEO_AGENT_COMPETITOR_URLS")
    competitor_domains_raw: str = Field(default_factory=lambda: _deploy("SEO_AGENT_COMPETITOR_DOMAINS", ""), alias="SEO_AGENT_COMPETITOR_DOMAINS")

    # ── Email notifications ───────────────────────────────────────────────────
    email_enabled: bool = Field(
        default_factory=lambda: str(_deploy("SEO_AGENT_EMAIL_ENABLED", _TOML.get("email", {}).get("enabled", False))).lower() in ("1", "true", "yes", "on")
    )
    email_smtp_host: str = Field(
        default_factory=lambda: _deploy("SEO_AGENT_EMAIL_SMTP_HOST", _TOML.get("email", {}).get("smtp_host", "smtp.gmail.com"))
    )
    email_smtp_port: int = Field(
        default_factory=lambda: int(_deploy("SEO_AGENT_EMAIL_SMTP_PORT", _TOML.get("email", {}).get("smtp_port", 587)))
    )
    email_smtp_user: str = Field(default_factory=lambda: _deploy("SEO_AGENT_EMAIL_SMTP_USER", ""), alias="SEO_AGENT_EMAIL_SMTP_USER")
    email_smtp_password: str = Field(default_factory=lambda: _deploy("SEO_AGENT_EMAIL_SMTP_PASSWORD", ""), alias="SEO_AGENT_EMAIL_SMTP_PASSWORD")
    email_from_address: str = Field(
        default_factory=lambda: _deploy("SEO_AGENT_EMAIL_FROM_ADDRESS", _TOML.get("email", {}).get("from_address", "seo-agent@example.com"))
    )
    email_from_name: str = Field(
        default_factory=lambda: _deploy("SEO_AGENT_EMAIL_FROM_NAME", _TOML.get("email", {}).get("from_name", "SEO Agent"))
    )
    # Stored as comma-separated string in env; list in TOML
    email_recipients_raw: str = Field(default_factory=lambda: _deploy("SEO_AGENT_EMAIL_RECIPIENTS", ""), alias="SEO_AGENT_EMAIL_RECIPIENTS")
    email_app_base_url: str = Field(
        default_factory=lambda: _deploy("SEO_AGENT_EMAIL_APP_BASE_URL", _TOML.get("email", {}).get("app_base_url", "http://localhost:8000")),
        alias="SEO_AGENT_EMAIL_APP_BASE_URL",
    )
    email_streamlit_url: str = Field(
        default_factory=lambda: _deploy("SEO_AGENT_STREAMLIT_URL", _TOML.get("email", {}).get("streamlit_url", "http://localhost:8501")),
        alias="SEO_AGENT_STREAMLIT_URL",
    )

    @property
    def email_recipients(self) -> list[str]:
        """Return merged list: env var (comma-separated) + TOML list."""
        from_env = [e.strip() for e in self.email_recipients_raw.split(",") if e.strip()]
        from_toml = _TOML.get("email", {}).get("recipients", [])
        # Deduplicate, preserve order
        seen: set[str] = set()
        result: list[str] = []
        for addr in from_toml + from_env:
            if addr not in seen:
                seen.add(addr)
                result.append(addr)
        return result

    # ── Scheduler ────────────────────────────────────────────────────────────
    audit_cron: str = Field(
        default_factory=lambda: _TOML.get("scheduler", {}).get("audit_cron", "0 2 * * *")
    )
    execution_cron: str = Field(
        default_factory=lambda: _TOML.get("scheduler", {}).get("execution_cron", "0 3 * * 1,4")
    )
    ranking_cron: str = Field(
        default_factory=lambda: _TOML.get("scheduler", {}).get("ranking_cron", "0 */6 * * *")
    )


    @property
    def competitor_urls(self) -> list[str]:
        """Return merged competitor URL seeds from TOML + env."""
        from_env = [u.strip() for u in self.competitor_urls_raw.split(",") if u.strip()]
        from_toml_raw = _TOML.get("competitive", {}).get("urls", [])
        from_toml = [str(u).strip() for u in from_toml_raw if str(u).strip()]
        seen: set[str] = set()
        result: list[str] = []
        for url in from_toml + from_env:
            if url not in seen:
                seen.add(url)
                result.append(url)
        return result

    @property
    def competitor_domains(self) -> list[str]:
        """Return merged competitor domain seeds from TOML + env."""
        from_env = [d.strip() for d in self.competitor_domains_raw.split(",") if d.strip()]
        from_toml_raw = _TOML.get("competitive", {}).get("domains", [])
        from_toml = [str(d).strip() for d in from_toml_raw if str(d).strip()]
        seen: set[str] = set()
        result: list[str] = []
        for domain in from_toml + from_env:
            if domain not in seen:
                seen.add(domain)
                result.append(domain)
        return result

@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
