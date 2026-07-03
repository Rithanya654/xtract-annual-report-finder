import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import smtplib
import re as _re
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
import tempfile
import threading
from datetime import datetime
from openpyxl import Workbook


def _safe_header(value: str, max_len: int = 100) -> str:
    """Strip CR/LF from user-supplied values used in email headers."""
    return _re.sub(r'[\r\n\t]', ' ', str(value or ""))[:max_len]

from shared import db, s3
from shared.reasons import clean_not_found_reason


def _recipients(run: dict) -> list[str]:
    """
    Build the recipient list: env NOTIFY_EMAILS (the guaranteed recipients, e.g.
    prashanth@xtract.io, jithesh@xtract.io) PLUS any per-run emails typed in the
    upload form. Deduplicated, order preserved.
    """
    env_emails = [e.strip() for e in os.environ.get("NOTIFY_EMAILS", "").split(",") if e.strip()]
    run_emails = [e.strip() for e in (run.get("notify_emails") or "").split(",") if e.strip()]
    seen, out = set(), []
    for e in env_emails + run_emails:
        key = e.lower()
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out


def send_completion_email(run_id: str):
    """Send completion email with Excel summary and PDF zip attached (local storage)."""
    tmp_path = None
    success = False
    try:
        run = db.get_run(run_id)
        if not run:
            print(f"Run {run_id} not found")
            return

        results = db.get_results(run_id)
        counts = db.get_run_counts(run_id)

        # Build Excel summary
        wb = Workbook()
        ws = wb.active
        ws.title = "Results"
        ws.append(["Agent Name", "Nickname", "Country", "Status", "Reason", "PDF URL", "S3 Key", "Landing URL", "Updated At"])
        for r in results:
            ws.append([
                r["agent"],
                r.get("nickname") or "",
                r["country"] or "",
                r["status"],
                clean_not_found_reason(r.get("error_msg")) if r["status"] == "not_found" else (r.get("error_msg") or ""),
                r["pdf_url"] or "",
                r["s3_key"] or "",
                r["landing_url"] or "",
                str(r["updated_at"]) if r["updated_at"] else ""
            ])
        with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp:
            wb.save(tmp.name)
            tmp_path = tmp.name

        # Upload xlsx to S3 and get presigned URL
        with open(tmp_path, 'rb') as f:
            xlsx_bytes = f.read()
        xlsx_key = f"{run_id}/export/summary.xlsx"
        s3.upload_bytes(xlsx_bytes, xlsx_key,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        xlsx_url = s3.get_presigned_url(xlsx_key)

        # Build zip on S3 and get a 7-day presigned link.
        # Never attach the zip to the email — runs with thousands of companies
        # produce GBs of PDFs which would exceed any SMTP size limit.
        _, zip_url = s3.build_run_zip(run_id)

        dashboard_url = os.environ.get("DASHBOARD_URL", "https://your-ec2-domain.com")
        smtp_host = os.environ.get("SMTP_HOST")
        smtp_port = int(os.environ.get("SMTP_PORT", 587))
        smtp_user = os.environ.get("SMTP_USER")
        smtp_pass = os.environ.get("SMTP_PASS")
        smtp_timeout = int(os.environ.get("SMTP_TIMEOUT_SECONDS", 60))
        notify_emails = _recipients(run)

        if not smtp_host or not notify_emails:
            print("SMTP not configured — skipping email")
            return

        safe_name = _safe_header(run['name'])
        subject = f"Xtract run complete — {counts['found']}/{counts['total']} found | {safe_name}"

        zip_line = (f"Download all PDFs (zip, 7-day link):\n{zip_url}"
                    if zip_url else "No PDFs found — zip not generated.")
        body = f"""Run: {safe_name}
Completed: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

Total companies: {counts['total']}
Found: {counts['found']}
Not found: {counts['not_found']}

{zip_line}

Download summary Excel (7-day link):
{xlsx_url}

Dashboard:
{dashboard_url}/run/{run_id}

Attached to this email: Excel summary.
"""

        msg = MIMEMultipart()
        msg['From'] = smtp_user
        msg['To'] = ", ".join(notify_emails)
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))

        # Attach Excel
        with open(tmp_path, 'rb') as f:
            part = MIMEBase('application', 'octet-stream')
            part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header('Content-Disposition', f'attachment; filename="xtract_{run_id}.xlsx"')
            msg.attach(part)

        with smtplib.SMTP(smtp_host, smtp_port, timeout=smtp_timeout) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)

        success = True
        db.mark_completion_email_sent(run_id)
        print(f"Completion email sent for run {run_id}")

    except Exception as e:
        print(f"Email send error: {e}")
    finally:
        if not success:
            db.release_completion_email_claim(run_id)
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


def trigger_completion_email_if_needed(run_id: str, *, async_send: bool = True):
    """Send the completion email once for a complete run, even from fallback paths."""
    if not db.claim_completion_email(run_id):
        return False

    if async_send:
        threading.Thread(target=send_completion_email, args=(run_id,), daemon=True).start()
    else:
        send_completion_email(run_id)
    return True


def send_landingpage_email(run_id: str):
    """Send landing page run completion email with Excel only (no PDFs)."""
    tmp_path = None
    try:
        run = db.get_run(run_id)
        if not run:
            print(f"Run {run_id} not found")
            return

        lp_results = db.get_landingpage_results(run_id)

        wb = Workbook()
        ws = wb.active
        ws.title = "Landing Pages"
        ws.append(["Agent", "Country", "Landing URL", "Sub URL", "URL Type", "Confidence", "Reachable", "Found At"])
        for r in lp_results:
            ws.append([
                r["agent"],
                r["country"] or "",
                r["landing_url"] or "",
                r["sub_url"] or "",
                r["url_type"] or "",
                r["confidence"] or "",
                r["reachable"] or "",
                str(r["found_at"]) if r["found_at"] else "",
            ])

        with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp:
            wb.save(tmp.name)
            tmp_path = tmp.name

        smtp_host = os.environ.get("SMTP_HOST")
        smtp_port = int(os.environ.get("SMTP_PORT", 587))
        smtp_user = os.environ.get("SMTP_USER")
        smtp_pass = os.environ.get("SMTP_PASS")
        smtp_timeout = int(os.environ.get("SMTP_TIMEOUT_SECONDS", 60))
        notify_emails = _recipients(run)
        dashboard_url = os.environ.get("DASHBOARD_URL", "https://your-ec2-domain.com")

        if not smtp_host or not notify_emails:
            print("SMTP not configured")
            return

        found_count = sum(1 for r in lp_results if r.get("url_type") not in ("not_found", "error"))
        safe_name = _safe_header(run['name'])
        subject = f"Landing page search complete — {safe_name} ({found_count} found)"

        body = f"""Run: {safe_name}
Completed: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

Total landing page entries: {len(lp_results)}
Found: {found_count}

Dashboard:
{dashboard_url}/run/{run_id}

Excel summary attached.
"""

        msg = MIMEMultipart()
        msg['From'] = smtp_user
        msg['To'] = ", ".join(notify_emails)
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))

        with open(tmp_path, 'rb') as f:
            part = MIMEBase('application', 'octet-stream')
            part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header('Content-Disposition', f'attachment; filename="landingpages_{run_id}.xlsx"')
            msg.attach(part)

        with smtplib.SMTP(smtp_host, smtp_port, timeout=smtp_timeout) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)

        print(f"Landing page email sent for run {run_id}")

    except Exception as e:
        print(f"Landing page email error: {e}")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
