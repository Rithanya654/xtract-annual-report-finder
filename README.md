# Xtract ARF — Distributed Annual Report Finder

Xtract ARF is a production-style document discovery system that finds, validates,
tracks, and exports annual report PDFs at scale. It combines a FastAPI dashboard,
Redis-backed work distribution, PostgreSQL state tracking, async Python workers,
AI-assisted URL discovery, local PDF validation, AWS S3 storage, and on-demand EC2
Spot worker orchestration.

The project was built around a real operational problem: process large
spreadsheets of companies, locate the correct annual report or financial
statement, reject weak matches, and keep the whole run observable from a browser.

## Why This Project Stands Out

- **Distributed queue architecture**: dashboard/API and worker processes are
  decoupled through Redis, so compute can scale horizontally.
- **Cloud cost-aware compute**: workers can be launched as one-time EC2 Spot
  instances when a dashboard action queues work, then stopped after the queue
  drains.
- **AI plus deterministic validation**: OpenAI/Gemini can suggest candidate
  pages, but every accepted document still passes local validation checks.
- **Financial-document specific logic**: accepts documents only when required
  statement evidence is present, with Balance Sheet / Statement of Financial
  Position as the key gate.
- **Resilient production operations**: stale job recovery, duplicate PDF hashing,
  run-level status tracking, clean failure reasons, and database-lock hardening.
- **Recruiter-relevant full-stack scope**: backend APIs, dashboard UX, async
  workers, cloud orchestration, database schema design, and operational tooling.

## Demo Flow

1. Upload an Excel/CSV file with company names and optional metadata.
2. The dashboard creates a run and pushes one job per company into Redis.
3. Workers consume jobs concurrently, discover candidate report URLs, download
   PDFs, validate company/year/statement evidence, and upload accepted PDFs to S3.
4. The dashboard shows live progress, found/not-found counts, clean rejection
   reasons, estimated cost, and export/download options.
5. When the queue is empty and no rows are pending/running, Spot workers can
   terminate automatically.

## Architecture

```text
                 ┌──────────────────────────────┐
                 │        Browser Dashboard      │
                 │  upload, monitor, export      │
                 └──────────────┬───────────────┘
                                │
                 ┌──────────────▼───────────────┐
                 │ FastAPI control application   │
                 │ runs, APIs, cost, spot control│
                 └───────┬──────────────┬───────┘
                         │              │
              ┌──────────▼─────┐  ┌─────▼──────────┐
              │ PostgreSQL      │  │ Redis queue     │
              │ run/result state│  │ xtract:jobs     │
              └──────────┬─────┘  └─────┬──────────┘
                         │              │
                 ┌───────▼──────────────▼───────┐
                 │ Async worker fleet             │
                 │ search, crawl, validate, upload│
                 └───────┬──────────────┬────────┘
                         │              │
             ┌───────────▼───────┐  ┌───▼────────────┐
             │ OpenAI / Gemini    │  │ AWS S3          │
             │ candidate reasoning│  │ PDF storage     │
             └───────────────────┘  └────────────────┘
```

## Core Design Decisions

### 1. Queue-First Processing

The dashboard does not perform expensive PDF discovery inline. It validates the
upload, creates run/result rows, and pushes jobs to Redis. Workers claim jobs
independently, which keeps the UI responsive and lets the system scale from one
local worker to a fleet of cloud workers.

### 2. Hybrid Search Pipeline

The search stack is intentionally layered:

- direct PDF and landing-page hints from the spreadsheet
- investor-relations path probing
- DDGS/web search queries
- HTML crawling and candidate PDF scoring
- optional Gemini fallback for hard rows
- OpenAI-assisted landing-page and document verification

This makes the pipeline robust when a company site is messy, multilingual, or
not indexed cleanly.

### 3. Validation Before Storage

The worker rejects candidates that look plausible but fail evidence checks. The
validation layer considers:

- company-name/domain matching
- expected year window
- junk URL and aggregator filtering
- PDF size and content sanity
- Balance Sheet / Statement of Financial Position evidence
- additional cash-flow handling for IFRS 17-style insurance reports

Only accepted PDFs are uploaded and deduplicated.

### 4. Spot Worker Orchestration

The control server can use `boto3` to launch tagged one-time Spot workers from a
worker AMI. The user-data script starts `xtract-worker@1..N` systemd services,
allowing each EC2 instance to run multiple worker processes. The same control
plane can list and terminate only the instances it owns.

### 5. Production Safety Work

The project includes several operational hardening details that matter in real
systems:

- SSRF protection for crawled/downloaded URLs
- PostgreSQL advisory locking around schema initialization
- workers skip DB migration by default to avoid startup lock storms
- stale job recovery support for interrupted workers
- SHA-256 PDF hash deduplication
- clean one-line failure reasons for dashboard/export/email
- estimated run-cost visibility for compute and API usage

## Tech Stack

| Area | Tools |
|---|---|
| Backend API | FastAPI, Uvicorn |
| Queue | Redis |
| Database | PostgreSQL, psycopg2 |
| Workers | Async Python, httpx, BeautifulSoup, pdfplumber |
| AI/Search | OpenAI, optional Gemini, DDGS |
| Cloud | AWS EC2 Spot, S3, boto3 |
| UI | Jinja templates, vanilla HTML/CSS/JS |
| Exports | CSV, XLSX, ZIP/presigned URLs |

## Repository Layout

```text
dashboard/      FastAPI app, templates, API routes, exports, email hooks
worker/         async job runner, search pipeline, crawler, PDF validation
shared/         database, Redis queue, S3, cost model, Spot worker orchestration
orchestrator/   spreadsheet parsing and chunking logic
scripts/        operational utilities such as stale job requeue
```

## Local Development

### 1. Create Environment

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill in at least:

```env
POSTGRES_URL=postgresql://xtract:xtract_local_password@localhost:5432/xtract
REDIS_URL=redis://localhost:6379/0
OPENAI_API_KEY=your-openai-key
S3_BUCKET=your-s3-bucket
```

### 2. Start Local Services

```bash
docker compose up -d
```

### 3. Run Dashboard

```bash
uvicorn dashboard.app:app --reload --host 0.0.0.0 --port 8000
```

Open:

```text
http://localhost:8000
```

### 4. Run Worker

In another terminal:

```bash
source venv/bin/activate
python -m worker.worker
```

## Input Format

The upload accepts Excel/CSV files. The minimum required column is a company
name. Several aliases are supported.

| Logical field | Example aliases |
|---|---|
| Company / Agent | `agent`, `agent name`, `company`, `company name`, `bank name` |
| Country | `country`, `countrycode`, `country code` |
| Domain | `domain`, `site`, `company domain` |
| Landing URL | `landing url`, `landingpageurl`, `website`, `url` |
| Statement URL | `statement_url`, `statement url` |
| Expected Year | `expectedreportyear`, `expected_year` |

## Main API Endpoints

```text
POST /api/upload
GET  /api/run/{run_id}/status
GET  /api/run/{run_id}/results
GET  /api/run/{run_id}/export/xlsx
GET  /api/run/{run_id}/export/csv
GET  /api/run/{run_id}/zip
POST /api/run/{run_id}/retry
POST /api/run/{run_id}/landingpage
GET  /api/spot-workers/status
POST /api/spot-workers/start
POST /api/spot-workers/stop
```

## Configuration Highlights

```env
OPENAI_LANDING_MODEL=gpt-4o-mini
OPENAI_VERIFY_MODEL=gpt-4o-mini
GEMINI_SEARCH_ENABLED=false
GEMINI_SEARCH_MODEL=gemini-3.5-flash
XTRACT_MAX_SECONDS_PER_COMPANY=180
XTRACT_EXTENDED_SEARCH_SECONDS=120

SPOT_WORKER_AUTO_START_ON_WORK=true
SPOT_WORKER_AUTO_STOP_ON_COMPLETE=true
SPOT_WORKER_AUTO_STOP_DELAY_SECONDS=60
XTRACT_WORKER_RUN_DB_INIT=false
```

`XTRACT_WORKER_RUN_DB_INIT=false` is intentional. In a distributed setup, the
control server owns schema initialization; workers should avoid DDL during
startup.

## What I Would Improve Next

- Add Playwright-based crawler fallback for JavaScript-heavy investor pages.
- Persist token usage from AI calls for exact cost accounting.
- Move deployment into Terraform or AWS CDK.
- Add integration tests around Redis queue behavior and worker interruption.
- Add a dashboard timeline for per-row search stages.

## Notes

This repository is presented as a portfolio-ready version of a production-style
system. Secrets, production IPs, and environment-specific deployment summaries
are intentionally excluded.
