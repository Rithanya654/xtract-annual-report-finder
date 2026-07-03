#!/usr/bin/env python3

import json
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from shared import db, queue


STALE_MINUTES = int(os.environ.get("STALE_JOB_MINUTES", "20"))


def requeue_stale_pdf_jobs() -> int:
    requeued = 0
    with db.get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    run_id,
                    agent,
                    COALESCE(country, ''),
                    COALESCE(domain, ''),
                    COALESCE(nickname, ''),
                    COALESCE(period_type, 'Year End'),
                    COALESCE(statement_url, ''),
                    COALESCE(landing_url, ''),
                    COALESCE(auto_id, ''),
                    COALESCE(client_id, ''),
                    COALESCE(expected_year, ''),
                    COALESCE(mob_id, '')
                FROM results
                WHERE status = 'running'
                  AND updated_at < NOW() - (%s || ' minutes')::interval
                ORDER BY updated_at ASC
                """,
                (STALE_MINUTES,),
            )
            rows = cur.fetchall()

            if not rows:
                return 0

            cur.execute(
                """
                UPDATE results
                SET status = 'pending', updated_at = NOW()
                WHERE status = 'running'
                  AND updated_at < NOW() - (%s || ' minutes')::interval
                """,
                (STALE_MINUTES,),
            )

    jobs: list[dict] = []
    for (
        run_id, agent, country, domain, nickname, period_type, statement_url,
        landing_url, auto_id, client_id, expected_year, mob_id
    ) in rows:
        jobs.append(
            {
                "type": "pdf",
                "run_id": run_id,
                "agent": agent,
                "country": country,
                "domain": domain,
                "nickname": nickname,
                "period_type": period_type,
                "statement_url": statement_url,
                "landing_url": landing_url,
                "auto_id": auto_id,
                "client_id": client_id,
                "expected_year": expected_year,
                "mob_id": mob_id,
            }
        )

    if jobs:
        queue.push_jobs(jobs)
        requeued = len(jobs)

    return requeued


def finalize_completed_runs() -> int:
    completed = 0
    with db.get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT run_id
                FROM results
                GROUP BY run_id
                HAVING SUM(CASE WHEN status IN ('pending', 'running') THEN 1 ELSE 0 END) = 0
                """
            )
            run_ids = [row[0] for row in cur.fetchall()]

    for run_id in run_ids:
        if db.mark_run_complete(run_id):
            completed += 1
            try:
                from dashboard.email_sender import trigger_completion_email_if_needed

                trigger_completion_email_if_needed(run_id, async_send=False)
            except Exception as exc:
                print(f"[stale-requeue] completion email trigger failed for {run_id}: {exc}")

    return completed


def retry_unsent_completion_emails() -> int:
    retried = 0
    with db.get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT run_id
                FROM runs
                WHERE status = 'complete'
                  AND completion_email_sent_at IS NULL
                ORDER BY created_at ASC
                """
            )
            run_ids = [row[0] for row in cur.fetchall()]

    for run_id in run_ids:
        try:
            from dashboard.email_sender import trigger_completion_email_if_needed

            if trigger_completion_email_if_needed(run_id, async_send=False):
                retried += 1
        except Exception as exc:
            print(f"[stale-requeue] unsent completion email retry failed for {run_id}: {exc}")

    return retried


def main() -> None:
    requeued = requeue_stale_pdf_jobs()
    completed = finalize_completed_runs()
    retried_unsent = retry_unsent_completion_emails()
    print(json.dumps({
        "requeued": requeued,
        "completed": completed,
        "retried_unsent": retried_unsent,
    }))


if __name__ == "__main__":
    main()
