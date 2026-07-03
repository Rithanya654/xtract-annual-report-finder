import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv
import uuid
import re
import tempfile
from openpyxl import Workbook
import csv as csv_module

_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE,
)


def _require_valid_run_id(run_id: str):
    if not _UUID_RE.match(run_id):
        raise HTTPException(status_code=400, detail="Invalid run_id format")

load_dotenv()

app = FastAPI()

@app.on_event("startup")
def startup():
    from shared import db
    db.db_init()

templates_dir = os.path.join(os.path.dirname(__file__), "templates")
templates = Jinja2Templates(directory=templates_dir)

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")

@app.get("/run/{run_id}", response_class=HTMLResponse)
async def dashboard(request: Request, run_id: str):
    return templates.TemplateResponse(request=request, name="dashboard.html", context={"run_id": run_id})

# ── API routes ────────────────────────────────────────────────────────────────

from shared import db, queue
from shared.costs import estimate_run_cost
from shared.reasons import clean_not_found_reason
from orchestrator import splitter


def _with_clean_reasons(results: list[dict]) -> list[dict]:
    cleaned = []
    for row in results:
        item = dict(row)
        if item.get("status") == "not_found":
            item["error_msg"] = clean_not_found_reason(item.get("error_msg"))
        cleaned.append(item)
    return cleaned

def _ensure_spot_workers_for_work() -> dict | None:
    try:
        from shared import spot_workers
        if spot_workers.auto_start_enabled():
            return spot_workers.ensure_workers()
    except Exception as exc:
        # Never discard queued work because compute launch failed; surface status in logs/UI.
        print(f"Spot worker auto-start skipped/failed: {exc}")
    return None

@app.post("/api/upload")
async def upload(
    file: UploadFile = File(...),
    run_name: str = Form(...),
    notify_emails: str = Form(""),
    run_type: str = Form("pdf"),          # "pdf" or "landingpage"
):
    """Upload Excel/CSV and start run."""
    ext = (file.filename or "").rsplit('.', 1)[-1].lower()
    if ext not in ('xlsx', 'xls', 'csv'):
        raise HTTPException(status_code=400, detail="File must be .xlsx, .xls, or .csv")

    if run_type not in ("pdf", "landingpage"):
        raise HTTPException(status_code=400, detail="run_type must be 'pdf' or 'landingpage'")

    with tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext}") as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        rows = splitter.parse_spreadsheet(tmp_path)

        if not rows:
            raise HTTPException(status_code=400, detail="No valid companies found in file")

        run_id = str(uuid.uuid4())
        db.create_run(run_id, run_name, len(rows), notify_emails=notify_emails)
        db.insert_results_bulk(run_id, rows)

        if run_type == "landingpage":
            db.set_run_lp_total(run_id, len(rows))

        for chunk in splitter.split_into_chunks(rows, 200):
            if run_type == "landingpage":
                jobs = [{
                    "type": "landingpage",
                    "run_id": run_id,
                    "agent": row.agent,
                    "country": row.country,
                    "nickname": row.nickname,
                } for row in chunk]
            else:
                jobs = [{
                    "type": "pdf",
                    "run_id": run_id,
                    "agent": row.agent,
                    "country": row.country,
                    "domain": row.domain,
                    "nickname": row.nickname,
                    "period_type": row.period_type,
                    "statement_url": row.statement_url,
                    "landing_url": row.landing_url,
                    "auto_id": row.auto_id,
                    "client_id": row.client_id,
                    "expected_year": row.expected_year,
                    "mob_id": row.mob_id,
                } for row in chunk]
            queue.push_jobs(jobs)

        spot_workers_started = _ensure_spot_workers_for_work()

        return {
            "run_id": run_id,
            "total": len(rows),
            "redirect_url": f"/run/{run_id}",
            "spot_workers": spot_workers_started,
        }

    finally:
        os.unlink(tmp_path)


@app.get("/api/run/{run_id}/status")
def get_status(run_id: str):
    _require_valid_run_id(run_id)
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    if run["status"] == "complete":
        from dashboard.email_sender import trigger_completion_email_if_needed
        trigger_completion_email_if_needed(run_id)

    counts = db.get_run_counts(run_id)
    total = counts["total"]
    pending = counts["pending"] + counts["running"]
    complete = total - pending
    percent = int((complete / total * 100) if total > 0 else 0)

    return {
        "run_id": run_id,
        "name": run.get("name", ""),
        "status": run["status"],
        "total": total,
        "found": counts["found"],
        "not_found": counts["not_found"],
        "pending": counts["pending"],
        "running": counts["running"],
        "percent": percent,
        "cost": estimate_run_cost(run, counts),
    }


@app.get("/api/run/{run_id}/results")
def get_results(run_id: str):
    _require_valid_run_id(run_id)
    return _with_clean_reasons(db.get_results(run_id))


@app.get("/api/run/{run_id}/export/xlsx")
def export_xlsx(run_id: str, background_tasks: BackgroundTasks):
    _require_valid_run_id(run_id)
    results = _with_clean_reasons(db.get_results(run_id))
    wb = Workbook()
    ws = wb.active
    ws.title = "Results"
    ws.append(["Agent Name", "Nickname", "Country", "Status", "Reason",
               "PDF URL", "S3 Key", "Landing URL", "Updated At"])
    for r in results:
        ws.append([
            r["agent"], r.get("nickname") or "", r.get("country") or "", r["status"],
            r.get("error_msg") or "",
            r.get("pdf_url") or "", r.get("s3_key") or "",
            r.get("landing_url") or "",
            str(r["updated_at"]) if r.get("updated_at") else ""
        ])
    with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp:
        wb.save(tmp.name)
        tmp_path = tmp.name
    background_tasks.add_task(os.unlink, tmp_path)
    return FileResponse(tmp_path, filename=f"xtract_{run_id}.xlsx",
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/api/run/{run_id}/export/csv")
def export_csv(run_id: str, background_tasks: BackgroundTasks):
    _require_valid_run_id(run_id)
    results = _with_clean_reasons(db.get_results(run_id))
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix=".csv", newline='') as tmp:
        writer = csv_module.writer(tmp)
        writer.writerow(["Agent Name", "Nickname", "Country", "Status", "Reason",
                         "PDF URL", "S3 Key", "Landing URL", "Updated At"])
        for r in results:
            writer.writerow([
                r["agent"], r.get("nickname") or "", r.get("country") or "", r["status"],
                r.get("error_msg") or "",
                r.get("pdf_url") or "", r.get("s3_key") or "",
                r.get("landing_url") or "",
                str(r["updated_at"]) if r.get("updated_at") else ""
            ])
        tmp_path = tmp.name
    background_tasks.add_task(os.unlink, tmp_path)
    return FileResponse(tmp_path, filename=f"xtract_{run_id}.csv", media_type="text/csv")


@app.post("/api/run/{run_id}/stop")
def stop_run(run_id: str):
    """Stop a running run: flush queue jobs and mark DB rows as not_found."""
    _require_valid_run_id(run_id)
    removed = queue.flush_run_jobs(run_id)
    db.stop_run(run_id)
    return {"stopped": True, "jobs_removed": removed}


@app.get("/api/run/{run_id}/zip")
def get_zip(run_id: str):
    _require_valid_run_id(run_id)
    from shared import s3
    url = s3.generate_run_zip_url(run_id)
    return {"url": url}


_SAFE_KEY_RE = re.compile(r'^[0-9a-f\-_a-zA-Z/][0-9a-f\-_a-zA-Z/.]*$')


@app.get("/api/presign")
def presign(key: str):
    """Return a presigned S3 URL for a single object."""
    if not _SAFE_KEY_RE.match(key) or '..' in key or key.startswith('/'):
        raise HTTPException(status_code=400, detail="Invalid storage key")
    from shared import s3
    return {"url": s3.get_presigned_url(key)}


@app.post("/api/run/{run_id}/landingpage")
def trigger_landingpage(run_id: str):
    """Queue landing page jobs for all companies in this run."""
    _require_valid_run_id(run_id)
    results = db.get_results(run_id)
    jobs = [{
        "type": "landingpage",
        "run_id": run_id,
        "agent": r["agent"],
        "country": r.get("country") or "",
        "nickname": r.get("nickname") or "",
    } for r in results]
    spot_workers_started = None
    if jobs:
        db.set_run_lp_total(run_id, len(jobs))
        queue.push_jobs(jobs)
        spot_workers_started = _ensure_spot_workers_for_work()
    return {"queued": len(jobs), "spot_workers": spot_workers_started}


@app.get("/api/run/{run_id}/landingpage/results")
def get_landingpage_results(run_id: str):
    _require_valid_run_id(run_id)
    return db.get_landingpage_results(run_id)


@app.post("/api/run/{run_id}/retry")
def retry_run(run_id: str):
    """Re-queue PDF jobs for all not_found and error companies."""
    _require_valid_run_id(run_id)
    results = db.get_results(run_id)
    jobs = []
    for r in results:
        if r["status"] in ("not_found", "error"):
            # Reset to pending so the completion check works correctly
            db.update_result(run_id, r["agent"], status="pending")
            jobs.append({
                "type": "pdf",
                "run_id": run_id,
                "agent": r["agent"],
                "country": r.get("country") or "",
                "domain": r.get("domain") or "",
                "nickname": r.get("nickname") or "",
                "period_type": r.get("period_type") or "Year End",
                "statement_url": r.get("statement_url") or "",
                "landing_url": r.get("landing_url") or "",
                "auto_id": r.get("auto_id") or "",
                "client_id": r.get("client_id") or "",
                "expected_year": r.get("expected_year") or "",
                "mob_id": r.get("mob_id") or "",
            })
    spot_workers_started = None
    if jobs:
        queue.push_jobs(jobs)
        spot_workers_started = _ensure_spot_workers_for_work()
    return {"queued": len(jobs), "spot_workers": spot_workers_started}


def _spot_error(exc: Exception):
    msg = str(exc)
    if isinstance(exc, HTTPException):
        raise exc
    raise HTTPException(status_code=500, detail=msg[:500])


@app.get("/api/spot-workers/status")
def spot_workers_status():
    try:
        from shared import spot_workers
        return spot_workers.list_workers()
    except Exception as exc:
        _spot_error(exc)


@app.post("/api/spot-workers/start")
def spot_workers_start():
    try:
        from shared import spot_workers
        return spot_workers.launch_workers()
    except Exception as exc:
        _spot_error(exc)


@app.post("/api/spot-workers/stop")
def spot_workers_stop():
    try:
        from shared import spot_workers
        return spot_workers.terminate_workers()
    except Exception as exc:
        _spot_error(exc)


@app.post("/api/cleardb/{run_id}")
def clear_db(run_id: str):
    _require_valid_run_id(run_id)
    with db.get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM results WHERE run_id = %s", (run_id,))
            deleted = cur.rowcount
            cur.execute("DELETE FROM pdf_hashes WHERE run_id = %s", (run_id,))
            deleted += cur.rowcount
            cur.execute("DELETE FROM landingpagedb WHERE run_id = %s", (run_id,))
            deleted += cur.rowcount
    return {"deleted": deleted}


@app.get("/health")
@app.get("/api/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
