# Automated SEO Agent

A production-grade multi-agent SEO automation system for WordPress. It crawls your site, audits every page, plans keyword and content improvements, generates optimized copy using Claude, and applies changes directly to WordPress — with a human approval gate before anything is written.

---

## Table of Contents

- [How It Works](#how-it-works)
- [Agent Pipeline](#agent-pipeline)
- [Safety Layers](#safety-layers)
- [Project Structure](#project-structure)
- [Prerequisites](#prerequisites)
- [WordPress Setup](#wordpress-setup)
- [Running Locally](#running-locally)
- [Local Runbook (No Docker)](#local-runbook-no-docker)
- [Testing a Single Page](#testing-a-single-page)
- [Running on a Server](#running-on-a-server)
- [Server Runbook (No Docker)](#server-runbook-no-docker)
- [Runtime Configuration](#runtime-configuration)
- [Configuration Reference](#configuration-reference)
- [Execution Modes](#execution-modes)
- [CLI Commands](#cli-commands)
- [Reverting Changes](#reverting-changes)

---

## How It Works

```
WordPress Site
      │
      ▼
 [Crawler]  →  fetches all pages via WP REST API + sitemap
      │
 [SEO Audit]  →  scores 35+ rules across 6 dimensions (meta, content, structure, schema, images, technical)
      │
 [Keyword Planner]  →  assigns primary/secondary/long-tail keywords per page, finds content gaps
      │
 [SERP Analysis]  →  analyses configured competitor seed URLs/domains and extracts word counts, H2s, schema types
      │
 [Content Generator]  →  generates optimized meta titles, descriptions, new content sections, schema markup
      │
 [Policy Gate]  →  checks change cooldowns, risk levels, homepage rules — blocks unsafe changes
      │
 [Human Review]  ←── PAUSE: email notification sent, pipeline waits for approval
      │
 [Fix Executor]  →  applies approved changes to WordPress (DOM-aware content merging)
      │
 [Validator]  →  verifies each change is live; auto-rolls back failures
      │
 [Ranking Monitor]  →  checks SERP positions, generates AI executive report
```

---

## Agent Pipeline

| Agent | File | What it does |
|-------|------|-------------|
| Crawler | `src/agents/crawler_agent.py` | Fetches all WP pages/posts via REST API + sitemap, concurrent crawl |
| SEO Audit | `src/agents/seo_audit_agent.py` | 35+ rules across meta, headings, content, images, schema, canonicals — scores 0–100 |
| Keyword Planner | `src/agents/keyword_planner_agent.py` | Claude assigns primary/long-tail keywords per page, identifies content gaps |
| SERP Analysis | `src/agents/serp_analysis_agent.py` | Pulls competitor signals from configured seed URLs/domains: word count, H2s, schema types, meta descriptions |
| Content Generator | `src/agents/content_generator_agent.py` | Generates meta titles, descriptions, new HTML sections, schema markup — deduplication-aware |
| Policy Gate | `src/agents/policy_gate_agent.py` | Enforces cooldown periods, risk rules, homepage protection, batch limits |
| Fix Executor | `src/agents/fix_executor_agent.py` | Applies changes via WP REST API; DOM-aware HTML merging; AI risk pre-flight check |
| Validator | `src/agents/validation_agent.py` | Re-fetches each changed page, verifies changes are live, triggers rollback on failure |
| Ranking Monitor | `src/agents/ranking_monitor_agent.py` | Tracks SERP positions, generates AI executive summary report |

All agents share a typed `SEOAgentState` and run as a `LangGraph StateGraph`.

---

## Safety Layers

Changes are never written blindly. Eight independent safety layers protect your site:

1. **Dry-run default** — nothing is written unless you explicitly set `mode = live`
2. **Human approval gate** — pipeline pauses after planning, resumes only after explicit approval
3. **Pre-change snapshots** — current field values are stored before every write for rollback
4. **Auto-rollback** — if validation fails after a change, the original value is restored automatically
5. **AI risk pre-flight** — Fix Executor asks Claude to review the full change set before applying
6. **Batch limits** — max 10 tasks per run (configurable in `src/config/default.toml`)
7. **Change interval cooldown** — same field cannot be changed again within 7 days (meta: 14 days, content: 21 days)
8. **Homepage protection** — homepage changes always require manual approval, never auto-execute

---

## Project Structure

```
automated_seo_agent/
├── src/
│   ├── agents/               # All 9 LangGraph agent nodes
│   │   ├── state.py          # Shared SEOAgentState TypedDict
│   │   ├── crawler_agent.py
│   │   ├── seo_audit_agent.py
│   │   ├── keyword_planner_agent.py
│   │   ├── serp_analysis_agent.py
│   │   ├── content_generator_agent.py
│   │   ├── policy_gate_agent.py
│   │   ├── fix_executor_agent.py
│   │   ├── validation_agent.py
│   │   └── ranking_monitor_agent.py
│   ├── graph/
│   │   └── seo_graph.py      # LangGraph StateGraph, routing, singleton compiled graph
│   ├── tools/
│   │   ├── wordpress_tools.py # WordPress REST API tool wrappers (ai_* meta fields)
│   │   ├── html_merger.py    # DOM-aware section merging (Gutenberg/Elementor/plain)
│   │   └── seo_tools.py      # SEO audit helper tools
│   ├── api/
│   │   └── routes.py         # FastAPI endpoints (trigger runs, approve plans)
│   ├── worker/
│   │   ├── celery_app.py     # Celery app + queue config
│   │   └── tasks.py          # Celery task definitions
│   ├── services/
│   │   └── policy_engine.py  # Change cooldown + risk policy logic
│   ├── database/
│   │   └── db.py             # SQLAlchemy async session + init
│   ├── config/
│   │   ├── settings.py       # Pydantic settings (reads deployment_config + defaults)
│   │   ├── default.toml      # Runtime defaults
│   │   └── deployment_config.py  # Docker/runtime config source
│   └── observability/
│       └── metrics.py        # Structlog + token usage tracking
├── wp-plugin/
│   └── seo-agent-meta.php    # WordPress mu-plugin: registers ai_* meta fields
├── migrations/               # Alembic DB migrations
├── tests/
├── run_single_page.py        # Local single-page test runner
├── debug_wp_meta.py          # Verify ai_* meta fields persist on WP
├── revert_last_run.py        # Restore all fields from last run's snapshots
├── docker-compose.yml        # Full production stack
├── Dockerfile
└── .env.example (legacy)
```

---

## Prerequisites

| Requirement | Version |
|------------|---------|
| Python | 3.11+ |
| Docker + Docker Compose | Latest (for server / full stack) |
| WordPress | Self-hosted, REST API enabled |
| Claude API key | `console.anthropic.com` |

---

## WordPress Setup

### 1. Enable REST API

The WordPress REST API is enabled by default on all modern WordPress installs. Verify it works:

```
https://yoursite.com/wp-json/wp/v2/pages
```

Should return a JSON array of pages.

### 2. Create an Application Password

This is how the agent authenticates — more secure than your main password.

1. Go to **WP Admin → Users → Your Profile**
2. Scroll to **Application Passwords**
3. Enter a name (e.g. `SEO Agent`) and click **Add New Application Password**
4. Copy the password shown (format: `xxxx xxxx xxxx xxxx xxxx xxxx`)

### 3. Install the SEO Agent Plugin

This is required for meta fields to persist. The plugin registers custom `ai_*` meta fields and injects them into `<head>` — bypassing Yoast protected keys.

```bash
# Copy the plugin to your WordPress mu-plugins directory
# mu-plugins load automatically — no activation needed
cp wp-plugin/seo-agent-meta.php /path/to/wordpress/wp-content/mu-plugins/
```

On cPanel/shared hosting: upload via File Manager to `wp-content/mu-plugins/`.

The plugin handles:
- `ai_meta_title` → renders as `<title>` and `og:title`
- `ai_meta_description` → renders as `<meta name="description">` and `og:description`
- `ai_canonical_url` → renders as `<link rel="canonical">`
- `ai_schema_json` → renders as `<script type="application/ld+json">`
- When `ai_meta_title` is set, Yoast head output is suppressed for that page

---

## Running Locally

### Step 1 — Clone and install

```bash
cd automated_seo_agent

# Create a virtual environment (recommended)
python -m venv seo_env
source seo_env/bin/activate        # Linux/Mac
# or: seo_env\Scripts\activate     # Windows

# Install dependencies
pip install -r requirements.txt

# Install the CLI tool
pip install -e .
```

### Step 2 — Configure environment

Open `src/config/deployment_config.py` and fill in at minimum:

```python
DOCKER_ENV = {
    "ANTHROPIC_API_KEY": "sk-ant-your-key-here",
    "WP_BASE_URL": "https://yoursite.com",
    "WP_USERNAME": "your_wp_username",
    "WP_APP_PASSWORD": "xxxx xxxx xxxx xxxx xxxx xxxx",
}
```

### Step 3 — Start PostgreSQL and Redis (via Docker)

```bash
docker compose up -d postgres redis

# Verify they are healthy
docker compose ps
```

If you don't want Docker at all, skip this step. The agent falls back to SQLite automatically when `POSTGRES_URL` is empty in `src/config/deployment_config.py`. Note: LangGraph checkpoint persistence (pause/resume for human review) requires PostgreSQL.

### Step 4 — Run database migrations

```bash
alembic upgrade head
```

### Step 5 — Verify WordPress connection

```bash
python -c "
import asyncio
from src.wordpress.client import WordPressClient
async def test():
    wp = WordPressClient()
    ok = await wp.check_connection()
    print('WordPress connected:', ok)
asyncio.run(test())
"
```

### Step 6 — Run a dry-run scan

```bash
seo-agent scan --url https://yoursite.com --mode dry_run
```

This crawls and audits your site but writes nothing to WordPress. Safe to run anytime.

### Step 7 — Run API + workers + Streamlit locally

Use this when you want the full UI workflow (`Current Run`, approval, history), not just one CLI command.

Open separate terminals for each process and keep them running.

#### Terminal A — Redis

Linux/Mac:

```bash
redis-server
```

Windows (via WSL):

```bash
wsl
redis-cli ping
# If it does not return PONG:
redis-server
```

#### Terminal B — API server

```bash
cd automated_seo_agent
source seo_env/bin/activate        # Linux/Mac
# or: .\seo_env\Scripts\Activate.ps1   # Windows PowerShell
seo-agent serve --host 0.0.0.0 --port 8000 --no-scheduler
```

#### Terminal C — Celery pipeline worker

Linux/Mac:

```bash
python -m celery -A src.worker.celery_app worker -Q seo_pipeline --concurrency 2 --loglevel INFO
```

Windows PowerShell (recommended flags on Windows):

```powershell
python -m celery -A src.worker.celery_app worker -Q seo_pipeline -P solo --concurrency 1 --loglevel INFO -n pipeline@%h
```

#### Terminal D — Celery monitor worker

Linux/Mac:

```bash
python -m celery -A src.worker.celery_app worker -Q seo_monitor --concurrency 4 --loglevel INFO
```

Windows PowerShell:

```powershell
python -m celery -A src.worker.celery_app worker -Q seo_monitor -P solo --concurrency 1 --loglevel INFO -n monitor@%h
```

#### Terminal E — Streamlit dashboard

```bash
streamlit run streamlit_app.py
```

Open `http://localhost:8501`, trigger a run, and watch node progress in `Current Run`.

### Local quick checks

```bash
# API health
curl http://localhost:8000/health

# Redis health
redis-cli ping
```

If a run stays `pending` with all nodes `Waiting`, the pipeline worker is not consuming `seo_pipeline`.

## Local Runbook (No Docker)

This is a concise copy/paste checklist for local development.

### One-time setup

```bash
cd automated_seo_agent
python -m venv seo_env
source seo_env/bin/activate        # Linux/Mac
# or: .\seo_env\Scripts\Activate.ps1   # Windows PowerShell
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt
python -m pip install -e .
alembic upgrade head
```

### Each time you start local stack

1. Start Redis.
2. Start API (`seo-agent serve ...`).
3. Start pipeline worker (`seo_pipeline` queue).
4. Start monitor worker (`seo_monitor` queue).
5. Start Streamlit (`streamlit run streamlit_app.py`).

### Common local issues

1. `seo-agent` command not found:
   run `python -m pip install -e .` again, or use `.\seo_env\Scripts\seo-agent.exe` on Windows.
2. `celery` command not found:
   run `python -m pip install -r requirements.txt`, then use `python -m celery ...`.
3. Windows `PermissionError [WinError 5]` in Celery pool:
   use `-P solo --concurrency 1`.
4. WSL `redis-server: command not found`:
   `sudo apt update && sudo apt install -y redis-server`.
5. `Could not create server TCP listening socket *:6379`:
   another Redis instance is already running; verify with `redis-cli ping`.

---

## Testing a Single Page

For development and testing without crawling the whole site, use `run_single_page.py`.

### Setup

Open `run_single_page.py` and set:

```python
TARGET_PAGE_URL = "https://yoursite.com/your-page-slug/"
PAGE_ID = 123        # WordPress post/page ID (found in WP Admin URL)
LIVE_MODE = False    # False = dry run, True = actually apply changes
```

> Find the page ID in WP Admin by hovering over the page title — the URL shows `post=123`.

### Run

```bash
python run_single_page.py
```

**Dry run output** (LIVE_MODE = False):
```
Mode: DRY_RUN | Approval: pending
SEO Score:   62/100
Issues:      8
Tasks:       5

DRY RUN — What WOULD be changed:
  [meta_title] Optimized Title for Your Page | Site Name
  [meta_description] Compelling description with primary keyword...
  [new_section] <h2>Why Choose Us</h2><p>...</p>
```

**Live output** (LIVE_MODE = True):
```
Mode: LIVE | Approval: approved
SEO Score:   62/100
Issues:      8
Tasks:       5

LIVE — Applied: 4 | Failed: 0
  [OK]   [meta_title] Optimized Title for Your Page | Site Name
  [OK]   [meta_description] Compelling description with keyword...
  [OK]   [new_section] <h2>Why Choose Us</h2><p>...</p>
  [OK]   [canonical_url] https://yoursite.com/your-page-slug/
```

Full results are saved to `single_page_result.json`.

### Verify meta fields are persisting

```bash
python debug_wp_meta.py
```

This reads, writes, and reads back all `ai_*` meta fields to confirm the WP plugin is working correctly.

---

## Running on a Server

The full production stack runs via Docker Compose: PostgreSQL, Redis, FastAPI, two Celery workers, and a Celery Beat scheduler — all in one command.

Recommended path: Docker Compose on Linux. If you need process-based deployment without Docker, see `Server Runbook (No Docker)` below.

### Step 1 — Server requirements

- Any Linux VPS (Ubuntu 22.04 recommended)
- 2 vCPU, 4 GB RAM minimum
- Docker + Docker Compose installed

```bash
# Install Docker on Ubuntu
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker
```

### Step 2 — Clone and configure

```bash
git clone <your-repo-url> /opt/seo-agent
cd /opt/seo-agent
nano src/config/deployment_config.py
```

Set values in `src/config/deployment_config.py` (single source).

Required keys in `src/config/deployment_config.py`:

- `PUBLIC_HOST`
- `ANTHROPIC_API_KEY`
- `WP_BASE_URL`
- `WP_USERNAME`
- `WP_APP_PASSWORD`

Example values:

```python
DOCKER_ENV = {
    # Required
    "ANTHROPIC_API_KEY": "sk-ant-your-key-here",
    "WP_BASE_URL": "https://yoursite.com",
    "WP_USERNAME": "your_wp_username",
    "WP_APP_PASSWORD": "xxxx xxxx xxxx xxxx xxxx xxxx",

    # Optional API auth
    "SEO_AGENT_API_SECRET": "change-this-to-a-random-secret",

    # Database mode
    # Postgres: set these URLs
    "POSTGRES_URL": "postgresql+asyncpg://seo_agent:your_strong_db_password@postgres:5432/seo_agent",
    "POSTGRES_SYNC_URL": "postgresql://seo_agent:your_strong_db_password@postgres:5432/seo_agent",
    "LANGGRAPH_CHECKPOINT_URL": "postgresql://seo_agent:your_strong_db_password@postgres:5432/seo_agent",
    "REDIS_URL": "redis://redis:6379/0",

    # SQLite fallback: set all three Postgres URLs to ""
    # "POSTGRES_URL": "",
    # "POSTGRES_SYNC_URL": "",
    # "LANGGRAPH_CHECKPOINT_URL": "",
}
```

### Docker Runtime Config (quick checklist)

These are the key values to set in `src/config/deployment_config.py`.

`PUBLIC_HOST`, `ANTHROPIC_API_KEY`, `WP_BASE_URL`, `WP_USERNAME`, `WP_APP_PASSWORD`.

Notes:
- `PUBLIC_HOST` is used to build approval links and CORS values.
- `SEO_AGENT_ALLOWED_ORIGINS` should be your exact frontend origin(s), comma-separated if multiple.
- If you put API/Streamlit behind HTTPS and a reverse proxy, use `https://...` URLs.
- `SEO_AGENT_API_SECRET` is optional. If unset, API endpoints are unauthenticated.

### Step 3 — Install the WordPress plugin

Upload `wp-plugin/seo-agent-meta.php` to your WordPress server's `wp-content/mu-plugins/` directory (create it if it doesn't exist).

### Step 4 — Run database migrations

```bash
# Start postgres first
docker compose up -d postgres

# Wait for it to be healthy, then run migrations
docker compose run --rm api alembic upgrade head
```

### Step 5 — Start everything

```bash
docker compose up -d
```

This starts 6 services:
- `postgres` — database
- `redis` — task queue
- `api` — FastAPI server on port 8000
- `worker_pipeline` — runs the LangGraph pipeline (heavy, concurrency 2)
- `worker_monitor` — runs ranking checks (light, concurrency 4)
- `beat` — Celery cron scheduler

### Step 6 — Verify

```bash
# Check all services are running
docker compose ps

# Check API is responding
curl http://localhost:8000/health

# Tail logs
docker compose logs -f api
docker compose logs -f worker_pipeline
```

### Step 7 — Expose via reverse proxy (optional)

To access the API from the internet, use nginx:

```nginx
server {
    server_name seo-agent.yourcompany.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

Then get an SSL certificate:
```bash
certbot --nginx -d seo-agent.yourcompany.com
```

### Trigger a pipeline run via API

```bash
curl -X POST http://your-server:8000/api/v1/pipeline/run \
  -H "Authorization: Bearer YOUR_API_SECRET" \
  -H "Content-Type: application/json" \
  -d '{
    "site_id": 1,
    "target_url": "https://yoursite.com",
    "mode": "live"
  }'
```

The pipeline will pause at the human review gate, send an approval email, and wait. Approve via API:

```bash
curl -X POST http://your-server:8000/api/v1/pipeline/{run_id}/approve \
  -H "Authorization: Bearer YOUR_API_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"reviewer":"ops","notes":"approved from API"}'
```

Fetch status and events:

```bash
curl -H "Authorization: Bearer YOUR_API_SECRET" \
  http://your-server:8000/api/v1/pipeline/{run_id}

curl -H "Authorization: Bearer YOUR_API_SECRET" \
  http://your-server:8000/api/v1/pipeline/{run_id}/events
```

### Automated scheduling

By default the scheduler (Celery Beat) runs on the following cron schedule (configurable in `src/config/default.toml`):

| Task | Schedule |
|------|----------|
| Audit + planning | Daily at 2:00 AM |
| Execute approved plans | Monday + Thursday at 3:00 AM |
| Ranking monitor | Every 6 hours |

### Useful Docker commands

```bash
# View logs for a service
docker compose logs -f worker_pipeline

# Restart a service
docker compose restart api

# Stop everything (keeps data)
docker compose down

# Full reset — wipes database volumes
docker compose down -v && docker compose up -d

# Scale pipeline workers
docker compose up -d --scale worker_pipeline=3
```

## Server Runbook (No Docker)

Use this only if Docker is not allowed. Commands below assume Ubuntu 22.04.

### Step 1 — Install system packages

```bash
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3-pip redis-server postgresql postgresql-contrib nginx
```

### Step 2 — Create app directory and Python environment

```bash
sudo mkdir -p /opt/seo-agent
sudo chown -R $USER:$USER /opt/seo-agent
git clone <your-repo-url> /opt/seo-agent
cd /opt/seo-agent
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt
python -m pip install -e .
```

### Step 3 — Configure database and runtime config

Create PostgreSQL DB/user:

```bash
sudo -u postgres psql
CREATE USER seo_agent WITH PASSWORD 'replace_with_strong_password';
CREATE DATABASE seo_agent OWNER seo_agent;
\q
```

Set values in `src/config/deployment_config.py` for your server setup:

```python
DOCKER_ENV = {
    "ANTHROPIC_API_KEY": "sk-ant-your-key-here",
    "WP_BASE_URL": "https://yoursite.com",
    "WP_USERNAME": "your_wp_username",
    "WP_APP_PASSWORD": "xxxx xxxx xxxx xxxx xxxx xxxx",
    "POSTGRES_URL": "postgresql+asyncpg://seo_agent:replace_with_strong_password@127.0.0.1:5432/seo_agent",
    "POSTGRES_SYNC_URL": "postgresql://seo_agent:replace_with_strong_password@127.0.0.1:5432/seo_agent",
    "LANGGRAPH_CHECKPOINT_URL": "postgresql://seo_agent:replace_with_strong_password@127.0.0.1:5432/seo_agent",
    "REDIS_URL": "redis://127.0.0.1:6379/0",
    "SEO_AGENT_ENV": "production",
    "SEO_AGENT_API_SECRET": "replace_with_random_secret",
}
```

Run migrations:

```bash
cd /opt/seo-agent
source .venv/bin/activate
alembic upgrade head
```

### Step 4 — Create systemd services

`/etc/systemd/system/seo-agent-api.service`

```ini
[Unit]
Description=SEO Agent API
After=network.target redis-server.service postgresql.service

[Service]
User=<your-linux-user>
WorkingDirectory=/opt/seo-agent
ExecStart=/opt/seo-agent/.venv/bin/seo-agent serve --host 0.0.0.0 --port 8000 --no-scheduler
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/seo-agent-worker-pipeline.service`

```ini
[Unit]
Description=SEO Agent Celery Pipeline Worker
After=network.target redis-server.service postgresql.service

[Service]
User=<your-linux-user>
WorkingDirectory=/opt/seo-agent
ExecStart=/opt/seo-agent/.venv/bin/python -m celery -A src.worker.celery_app worker -Q seo_pipeline --concurrency 2 --loglevel INFO
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/seo-agent-worker-monitor.service`

```ini
[Unit]
Description=SEO Agent Celery Monitor Worker
After=network.target redis-server.service postgresql.service

[Service]
User=<your-linux-user>
WorkingDirectory=/opt/seo-agent
ExecStart=/opt/seo-agent/.venv/bin/python -m celery -A src.worker.celery_app worker -Q seo_monitor --concurrency 4 --loglevel INFO
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/seo-agent-beat.service`

```ini
[Unit]
Description=SEO Agent Celery Beat
After=network.target redis-server.service

[Service]
User=<your-linux-user>
WorkingDirectory=/opt/seo-agent
ExecStart=/opt/seo-agent/.venv/bin/python -m celery -A src.worker.celery_app beat --loglevel INFO
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now redis-server postgresql
sudo systemctl enable --now seo-agent-api seo-agent-worker-pipeline seo-agent-worker-monitor seo-agent-beat
```

Verify:

```bash
systemctl status seo-agent-api
systemctl status seo-agent-worker-pipeline
systemctl status seo-agent-worker-monitor
journalctl -u seo-agent-api -f
```

### Step 5 — Reverse proxy and SSL (recommended)

Use nginx to proxy `:8000`, then issue TLS cert via certbot.

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d seo-agent.yourcompany.com
```

---

### Trigger a pipeline run via API (same endpoints as Docker setup)

```bash
curl -X POST http://your-server:8000/api/v1/pipeline/run \
  -H "Authorization: Bearer YOUR_API_SECRET" \
  -H "Content-Type: application/json" \
  -d '{
    "site_id": 1,
    "target_url": "https://yoursite.com",
    "mode": "dry_run"
  }'
```

---

## Runtime Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `ANTHROPIC_API_KEY` | Yes | Claude API key |
| `WP_BASE_URL` | Yes | WordPress site URL |
| `WP_USERNAME` | Yes | WordPress username |
| `WP_APP_PASSWORD` | Yes | WordPress Application Password |
| `POSTGRES_URL` | Server | SQLAlchemy async DB URL |
| `POSTGRES_SYNC_URL` | Server | SQLAlchemy sync DB URL (Celery/Alembic) |
| `LANGGRAPH_CHECKPOINT_URL` | Server | LangGraph checkpoint DB URL |
| `REDIS_URL` | Server | Redis connection URL |
| `SERP_API_KEY` | Optional | SerpAPI key for ranking lookups (if used) |
| `SEO_AGENT_COMPETITOR_URLS` | Optional | Comma-separated competitor page URLs for free competitive analysis |
| `SEO_AGENT_COMPETITOR_DOMAINS` | Optional | Comma-separated competitor domains for free competitive analysis |
| `GOOGLE_PAGESPEED_API_KEY` | Optional | Google PageSpeed API key |
| `SEO_AGENT_API_SECRET` | Optional | Bearer token for API authentication |
| `PUBLIC_HOST` | Docker Compose | Public server hostname or IP (used to build Streamlit/API URLs in compose) |
| `SEO_AGENT_ALLOWED_ORIGINS` | Optional | Comma-separated CORS allowlist for FastAPI (set in production) |
| `SEO_AGENT_STREAMLIT_URL` | Optional | Streamlit URL used in approval email links |
| `SEO_AGENT_EMAIL_APP_BASE_URL` | Optional | Base API/app URL used in approval email links |
| `SEO_AGENT_ENV` | Optional | `development` or `production` |
| `SEO_AGENT_EMAIL_ENABLED` | Optional | Enable approval email notifications |
| `SEO_AGENT_EMAIL_RECIPIENTS` | Optional | Comma-separated approval email list |

Full list with defaults in `src/config/deployment_config.py` and `src/config/default.toml`.

---

## Configuration Reference

Runtime defaults live in `src/config/default.toml`, and deployment values are set in `src/config/deployment_config.py`.

Key settings:

```toml
[execution]
mode = "dry_run"          # dry_run | preview | live

[scoring]
max_batch_size = 10       # Max tasks applied per run
min_change_interval_days = 7  # Cooldown between changes to same field

[policy]
meta_cooldown_days = 14       # Meta title/desc cooldown
content_cooldown_days = 21    # Content section cooldown
homepage_manual_only = true   # Homepage always requires manual approval

[scheduler]
audit_cron = "0 2 28-31 * *"      # Month-end audit at 2 AM (guarded to true last day)
execution_cron = "0 3 * * 1,4"    # Execute Mon + Thu at 3 AM
ranking_cron = "0 */6 * * *"      # Ranking check every 6 hours
```

---

## Execution Modes

| Mode | Crawls | Plans | Writes to WP | Needs Approval |
|------|--------|-------|-------------|----------------|
| `dry_run` | Yes | Yes | No | No |
| `preview` | Yes | Yes | Simulated only | No |
| `live` | Yes | Yes | Yes | Yes (human gate) |

Default is `dry_run`. For one-off runs, pass `--mode live` to CLI. For scheduled/default behavior, set `[execution].mode` in `src/config/default.toml`.

---

## CLI Commands

```bash
# Dry-run scan (read-only, safe anytime)
seo-agent scan --url https://yoursite.com --mode dry_run

# Preview what would change (no writes)
seo-agent scan --url https://yoursite.com --mode preview

# Full pipeline with live writes
seo-agent scan --url https://yoursite.com --mode live

# Start the API server
seo-agent serve
seo-agent serve --host 0.0.0.0 --port 8000

# Show resolved configuration
seo-agent show-config
```

---

## Reverting Changes

### Option 1 — Automatic rollback

If the validator detects a change did not apply correctly, it automatically restores the previous value from the snapshot taken before the change.

### Option 2 — Revert last run manually

```bash
python revert_last_run.py
```

This reads `single_page_result.json` (created by `run_single_page.py`), finds all pre-change snapshots, and calls the WordPress API to restore every field that was changed.

### Option 3 — Manual revert via WordPress Admin

All changes are visible in WP Admin. Meta fields are stored as custom post meta (`ai_meta_title`, `ai_meta_description`, etc.) and can be cleared from the Custom Fields panel on any page/post.

---

## Cost Estimate

Each full pipeline run makes approximately 5–10 LLM calls per page (audit, keywords, content, policy, risk check). Costs per run with Claude Sonnet 4.6:

| Pages | Approx. Cost |
|-------|-------------|
| 1 page | ~$0.05 |
| 10 pages | ~$0.30–0.50 |
| 50 pages | ~$1.50–2.50 |
| 100 pages | ~$3–5 |

Seed-based SERP analysis can run with zero external API cost.


## To setup it completely a config file with proper env values in the below format have to be added to the config folder:

"""Single source of runtime configuration for Docker deployment.

The app (API/workers) reads these values directly at runtime.
"""

DOCKER_ENV: dict[str, str] = {
    # Core infra
    "POSTGRES_PASSWORD": "change_me_db_password",
    "PUBLIC_HOST": "your-server-ip-or-domain",
    "SEO_AGENT_ENV": "production",
    "LOG_LEVEL": "INFO",
    # Database mode:
    # - Postgres mode: set all three URLs below.
    # - SQLite fallback mode: leave all three empty ("").
    "POSTGRES_URL": "",
    "POSTGRES_SYNC_URL": "",
    "LANGGRAPH_CHECKPOINT_URL": "",
    "REDIS_URL": "redis://redis:6379/0",

    # LLM / SEO integrations
    "ANTHROPIC_API_KEY": "",
    "OPENAI_API_KEY": "",
    "GOOGLE_PAGESPEED_API_KEY": "",
    "SERP_API_KEY": "",

    # WordPress
    "WP_BASE_URL": "",
    "WP_USERNAME": "admin",
    "WP_APP_PASSWORD": "",
    "SEO_AGENT_WP_MAX_PAGES": "200",

    # Optional competitor seeds
    "SEO_AGENT_COMPETITOR_URLS": "",
    "SEO_AGENT_COMPETITOR_DOMAINS": "",

    # Email / approval notifications
    "SEO_AGENT_EMAIL_ENABLED": "true",
    "SEO_AGENT_EMAIL_SMTP_HOST": "smtp.gmail.com",
    "SEO_AGENT_EMAIL_SMTP_PORT": "587",
    "SEO_AGENT_EMAIL_SMTP_USER": "",
    "SEO_AGENT_EMAIL_SMTP_PASSWORD": "",
    "SEO_AGENT_EMAIL_FROM_ADDRESS": "",
    "SEO_AGENT_EMAIL_FROM_NAME": "SEO Agent",
    "SEO_AGENT_EMAIL_RECIPIENTS": "",

    # Optional API auth
    "SEO_AGENT_API_SECRET": "",
    "SEO_AGENT_ALLOWED_ORIGINS": "http://your-server-ip-or-domain:8501",
    "SEO_AGENT_EMAIL_APP_BASE_URL": "http://your-server-ip-or-domain:8000",
    "SEO_AGENT_STREAMLIT_URL": "http://your-server-ip-or-domain:8501",
}






