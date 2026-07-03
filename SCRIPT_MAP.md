# Script Map

Quick one-line guide to what each important file does.

## App Runtime

| File | What it does |
| --- | --- |
| `dashboard/app.py` | Main FastAPI app: upload page, run dashboard, API routes, DB initialization, export/stop/retry actions. |
| `dashboard/email_sender.py` | Sends completion emails with Excel summaries and S3 download links. |
| `dashboard/templates/index.html` | Browser upload screen for Excel/CSV files and run type selection. |
| `dashboard/templates/dashboard.html` | Browser dashboard that polls run status/results and exposes action buttons. |
| `orchestrator/splitter.py` | Parses uploaded Excel/CSV files into company rows and chunks them for queueing. |
| `worker/worker.py` | Redis worker loop that processes PDF and landing-page jobs. |
| `worker/search.py` | Finds, scores, downloads, and verifies annual report PDF candidates. |
| `worker/crawler.py` | Finds investor-relations landing pages and crawls financial/report links. |
| `worker/pdf_validator.py` | Validates PDFs for company match, report year, statements, junk URLs, and scoring signals. |
| `shared/db.py` | PostgreSQL schema setup and all run/result/hash/landing-page database operations. |
| `shared/queue.py` | Redis queue helpers for pushing, claiming, flushing, and counting jobs. |
| `shared/s3.py` | S3 upload, presigned URL, byte upload, and run ZIP creation helpers. |
| `shared/models.py` | Dataclasses used to pass company rows and result-shaped data around the app. |

## Setup And Dependencies

| File | What it does |
| --- | --- |
| `requirements.txt` | Full dependency list for running dashboard and workers together. |
| `dashboard/requirements.txt` | Older/narrow dependency list for dashboard-only installs. |
| `worker/requirements.txt` | Older/narrow dependency list for worker-only installs. |
| `orchestrator/requirements.txt` | Older/narrow dependency list for upload/parsing/API pieces. |
| `docker-compose.yml` | Local-only Redis and Postgres containers; Postgres is exposed on host port `5433`. |

## Deployment

| File | What it does |
| --- | --- |
| `deploy/EC2-1-CONTROL/setup.sh` | Sets up the control EC2 with dashboard service, Postgres/Redis assumptions, and nginx config. |
| `deploy/EC2-1-CONTROL/xtract-dashboard.service` | systemd unit for running `uvicorn dashboard.app:app` on the control server. |
| `deploy/EC2-1-CONTROL/nginx.conf` | nginx reverse-proxy config for the dashboard server. |
| `deploy/EC2-2-WORKER/setup.sh` | Sets up worker server 2 with systemd worker instances. |
| `deploy/EC2-2-WORKER/start-workers.sh` | Starts five worker instances on worker server 2. |
| `deploy/EC2-2-WORKER/xtract-worker@.service` | systemd template for numbered worker processes on worker server 2. |
| `deploy/EC2-3-WORKER/setup.sh` | Sets up worker server 3 with systemd worker instances. |
| `deploy/EC2-3-WORKER/start-workers.sh` | Starts five worker instances on worker server 3. |
| `deploy/EC2-3-WORKER/xtract-worker@.service` | systemd template for numbered worker processes on worker server 3. |
| `deploy/setup-server.sh` | Legacy/general setup script for the server role. |
| `deploy/setup-worker.sh` | Legacy/general setup script for a worker role. |

## Package Markers

| File | What it does |
| --- | --- |
| `dashboard/__init__.py` | Marks `dashboard` as a Python package. |
| `orchestrator/__init__.py` | Marks `orchestrator` as a Python package. |
| `shared/__init__.py` | Marks `shared` as a Python package. |
| `worker/__init__.py` | Marks `worker` as a Python package. |

## Docs

| File | What it does |
| --- | --- |
| `README.md` | Main local-development and architecture guide. |
| `DEPLOY.md` | Production deployment and operations guide. |
| `DEPLOYMENT_SUMMARY.md` | Current AWS handoff inventory with secrets redacted. |
| `SCRIPT_MAP.md` | This file: quick one-line explanation of repo files. |
