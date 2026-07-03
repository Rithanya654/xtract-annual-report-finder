import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import asyncio
import threading
import re
import time
from dotenv import load_dotenv
from hashlib import sha256

load_dotenv()

from shared import db, queue, s3
from shared.models import CompanyRow
from shared.reasons import clean_not_found_reason
from worker.search import find_annual_report
from worker.crawler import find_landing_url, crawl_financial_links
from worker.pdf_validator import pdf_has_required_statements

HEARTBEAT_INTERVAL_SECONDS = 60



def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def trigger_spot_auto_stop_if_idle(run_id: str):
    """Terminate dashboard-managed Spot workers after all queued/running work drains."""
    if not _env_bool("SPOT_WORKER_AUTO_STOP_ON_COMPLETE", False):
        return

    delay = max(0, _env_int("SPOT_WORKER_AUTO_STOP_DELAY_SECONDS", 60))

    def _stop():
        try:
            if delay:
                time.sleep(delay)
            queued = queue.queue_length()
            active = db.get_active_work_counts()
            if queued or active["pending"] or active["running"]:
                print(
                    f"Spot auto-stop skipped after run {run_id}: "
                    f"queued={queued}, pending={active['pending']}, running={active['running']}"
                )
                return

            from shared.spot_workers import terminate_workers
            result = terminate_workers()
            print(f"Spot auto-stop after run {run_id}: {result}")
        except Exception as e:
            print(f"Spot auto-stop error after run {run_id}: {e}")

    threading.Thread(target=_stop, daemon=True).start()


async def _running_heartbeat(run_id: str, agent: str):
    """Refresh updated_at for long-running jobs so only genuinely dead work goes stale."""
    try:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
            db.update_result(run_id, agent, status="running")
            print(f"[{agent}] Heartbeat")
    except asyncio.CancelledError:
        raise


async def _await_with_heartbeat(coro, run_id: str, agent: str):
    heartbeat_task = asyncio.create_task(_running_heartbeat(run_id, agent))
    try:
        return await coro
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass


async def process_job(job: dict):
    """Process a single job. Exceptions are caught per-job so the loop survives."""
    job_type = job.get("type", "pdf")
    run_id = job["run_id"]
    agent = job["agent"]

    run = db.get_run(run_id)
    if run and run.get("status") == "stopped":
        print(f"[{agent}] Run {run_id} is stopped — skipping")
        return

    if job_type == "pdf":
        db.update_result(run_id, agent, status="running")

        try:
            row = CompanyRow(
                agent=agent,
                country=job.get("country", ""),
                domain=job.get("domain", ""),
                nickname=job.get("nickname", ""),
                period_type=job.get("period_type", "Year End"),
                statement_url=job.get("statement_url", ""),
                landing_url=job.get("landing_url", ""),
                auto_id=job.get("auto_id", ""),
                client_id=job.get("client_id", ""),
                expected_year=job.get("expected_year", ""),
                mob_id=job.get("mob_id", ""),
            )

            pdf_bytes, pdf_url, reject_reason = await _await_with_heartbeat(
                find_annual_report(row), run_id, agent
            )

            if pdf_bytes:
                ok, statements = pdf_has_required_statements(pdf_bytes)
                if not ok:
                    print(f"[{agent}] Rejected after statement validation: {statements}")
                    db.update_result(run_id, agent, status="not_found",
                                     error_msg=clean_not_found_reason(statements))
                else:
                    h = sha256(pdf_bytes).hexdigest()
                    if db.hash_exists(h):
                        print(f"[{agent}] Duplicate PDF hash — marking found")
                        db.update_result(
                            run_id, agent, status="found",
                            pdf_url=pdf_url, statements=statements
                        )
                    else:
                        db.save_hash(h, agent, pdf_url, run_id)

                        filename = re.sub(r'[^a-zA-Z0-9_-]', '_', agent)[:50] + "_annual_report.pdf"
                        s3_key = s3.upload_pdf(pdf_bytes, run_id, agent, filename)

                        db.update_result(
                            run_id, agent, status="found",
                            pdf_url=pdf_url, s3_key=s3_key,
                            statements=statements
                        )
                        print(f"[{agent}] Found and uploaded: {pdf_url}")
            else:
                db.update_result(run_id, agent, status="not_found",
                                 error_msg=clean_not_found_reason(reject_reason))
                print(f"[{agent}] Not found")

        except Exception as e:
            print(f"[{agent}] Error: {e}")
            db.update_result(run_id, agent, status="error", error_msg=str(e)[:500])

    elif job_type == "landingpage":
        db.update_result(run_id, agent, status="running")
        try:
            country = job.get("country", "")

            landing_url, confidence = await _await_with_heartbeat(
                find_landing_url(agent, country), run_id, agent
            )

            if landing_url:
                sub_links = await _await_with_heartbeat(
                    crawl_financial_links(landing_url, agent), run_id, agent
                )
                if sub_links:
                    for link in sub_links:
                        db.save_landingpage(
                            run_id, agent, country,
                            landing_url, link["url"], link["url_type"],
                            confidence, link["reachable"]
                        )
                    print(f"[{agent}] Landing page: {len(sub_links)} links found")
                else:
                    db.save_landingpage(
                        run_id, agent, country,
                        landing_url, landing_url, "ir_page",
                        confidence, "Y"
                    )
                    print(f"[{agent}] Landing page found, no sub-links")
                db.update_result(run_id, agent, status="found", landing_url=landing_url)
            else:
                db.save_landingpage(run_id, agent, country, "", "", "not_found", "", "N")
                db.update_result(run_id, agent, status="not_found")
                print(f"[{agent}] Landing page not found")

        except Exception as e:
            print(f"[{agent}] Landing page error: {e}")
            db.save_landingpage(run_id, agent, job.get("country", ""),
                                "", "", "error", "", "N")
            db.update_result(run_id, agent, status="error", error_msg=str(e)[:500])
        finally:
            lp_done, lp_total = db.increment_lp_done(run_id)
            if lp_total > 0 and lp_done >= lp_total:
                if db.mark_run_complete(run_id):
                    print(f"Landing page run {run_id} complete: {lp_done}/{lp_total}")
                    trigger_landingpage_email(run_id)
                    trigger_spot_auto_stop_if_idle(run_id)

    # Check if run is complete (only for pdf jobs that affect pending/running counts)
    if job_type == "pdf":
        counts = db.get_run_counts(run_id)
        if counts["pending"] == 0 and counts["running"] == 0:
            if db.mark_run_complete(run_id):
                print(f"Run {run_id} complete: {counts['found']} found, {counts['not_found']} not found")
                trigger_completion_email(run_id)
                trigger_spot_auto_stop_if_idle(run_id)


async def worker_loop():
    """Main worker loop. Runs claim_job in an executor to avoid blocking the event loop."""
    print("Worker started")
    print("Waiting for jobs from Redis queue (xtract:jobs)...")
    print("Press Ctrl+C to stop\n")

    loop = asyncio.get_running_loop()

    while True:
        try:
            # Run blocking BLPOP in a thread executor so the event loop stays alive
            job = await loop.run_in_executor(None, queue.claim_job)

            if job:
                await process_job(job)

        except KeyboardInterrupt:
            print("\nWorker stopped by user")
            break
        except Exception as e:
            print(f"Worker loop error: {e}")
            await asyncio.sleep(5)


if __name__ == "__main__":
    if _env_bool("XTRACT_WORKER_RUN_DB_INIT", False):
        db.db_init()
    asyncio.run(worker_loop())
