import psycopg2
import psycopg2.extras
import os
from contextlib import contextmanager
from typing import Optional


COMPLETION_EMAIL_CLAIM_TTL_MINUTES = int(
    os.environ.get("COMPLETION_EMAIL_CLAIM_TTL_MINUTES", "30")
)

@contextmanager
def get_db():
    conn = psycopg2.connect(os.environ["POSTGRES_URL"])
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def _add_column_if_missing(cur, table: str, column: str, definition: str):
    """Add a column only when metadata says it is absent, avoiding repeated DDL locks."""
    cur.execute("""
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = %s
          AND column_name = %s
    """, (table, column))
    if cur.fetchone():
        return
    cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

def db_init():
    """Create all tables on startup."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext('xtract_db_init'))")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS runs (
                    run_id      TEXT PRIMARY KEY,
                    name        TEXT,
                    status      TEXT DEFAULT 'running',
                    total       INTEGER DEFAULT 0,
                    found       INTEGER DEFAULT 0,
                    not_found   INTEGER DEFAULT 0,
                    created_at  TIMESTAMPTZ DEFAULT NOW(),
                    finished_at TIMESTAMPTZ
                );
            """)
            
            cur.execute("""
                CREATE TABLE IF NOT EXISTS results (
                    id          SERIAL PRIMARY KEY,
                    run_id      TEXT NOT NULL REFERENCES runs(run_id),
                    agent       TEXT NOT NULL,
                    country     TEXT,
                    domain      TEXT,
                    nickname    TEXT,
                    period_type TEXT,
                    statement_url TEXT,
                    status      TEXT DEFAULT 'pending',
                    pdf_url     TEXT,
                    s3_key      TEXT,
                    landing_url TEXT,
                    auto_id     TEXT,
                    client_id   TEXT,
                    source_url_id TEXT,
                    expected_year TEXT,
                    mob_id      TEXT,
                    region      TEXT,
                    statements  TEXT,
                    error_msg   TEXT,
                    updated_at  TIMESTAMPTZ DEFAULT NOW()
                );
            """)
            
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_results_run_id ON results(run_id);
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_results_status ON results(status);
            """)
            
            cur.execute("""
                CREATE TABLE IF NOT EXISTS pdf_hashes (
                    hash        TEXT PRIMARY KEY,
                    agent       TEXT NOT NULL,
                    pdf_url     TEXT,
                    run_id      TEXT,
                    created_at  TIMESTAMPTZ DEFAULT NOW()
                );
            """)
            
            cur.execute("""
                CREATE TABLE IF NOT EXISTS landingpagedb (
                    id          SERIAL PRIMARY KEY,
                    run_id      TEXT,
                    agent       TEXT NOT NULL,
                    country     TEXT,
                    landing_url TEXT,
                    sub_url     TEXT,
                    url_type    TEXT,
                    confidence  TEXT,
                    reachable   TEXT,
                    found_at    TIMESTAMPTZ DEFAULT NOW()
                );
            """)
            # Add landing-page/job metadata columns only when absent to avoid repeated DDL locks.
            _add_column_if_missing(cur, "runs", "lp_total", "INTEGER DEFAULT 0")
            _add_column_if_missing(cur, "runs", "lp_done", "INTEGER DEFAULT 0")
            _add_column_if_missing(cur, "runs", "notify_emails", "TEXT DEFAULT ''")
            _add_column_if_missing(cur, "runs", "completion_email_claimed_at", "TIMESTAMPTZ")
            _add_column_if_missing(cur, "runs", "completion_email_sent_at", "TIMESTAMPTZ")
            _add_column_if_missing(cur, "results", "domain", "TEXT")
            _add_column_if_missing(cur, "results", "nickname", "TEXT")
            _add_column_if_missing(cur, "results", "period_type", "TEXT")
            _add_column_if_missing(cur, "results", "statement_url", "TEXT")
            _add_column_if_missing(cur, "results", "auto_id", "TEXT")
            _add_column_if_missing(cur, "results", "client_id", "TEXT")
            _add_column_if_missing(cur, "results", "source_url_id", "TEXT")
            _add_column_if_missing(cur, "results", "expected_year", "TEXT")
            _add_column_if_missing(cur, "results", "mob_id", "TEXT")
            _add_column_if_missing(cur, "results", "region", "TEXT")

def create_run(run_id: str, name: str, total: int, notify_emails: str = ""):
    """Insert a new run row. notify_emails is a comma-separated string (optional)."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO runs (run_id, name, total, notify_emails)
                VALUES (%s, %s, %s, %s)
            """, (run_id, name, total, notify_emails or ""))

def insert_results_bulk(run_id: str, rows: list):
    """Bulk insert all companies as status=pending."""
    with get_db() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, """
                INSERT INTO results (
                    run_id, agent, country, domain, nickname, period_type, statement_url,
                    landing_url, auto_id, client_id, source_url_id, expected_year, mob_id,
                    region, status
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending'
                )
            """, [(
                run_id, row.agent, row.country, row.domain, row.nickname, row.period_type,
                row.statement_url, row.landing_url, row.auto_id, row.client_id,
                row.source_url_id, row.expected_year, row.mob_id, row.region
            ) for row in rows])

def update_result(run_id: str, agent: str, status: str, pdf_url: str = "", 
                  s3_key: str = "", landing_url: str = "", statements: str = "", 
                  error_msg: str = ""):
    """Update a result row."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE results
                SET status = %s, pdf_url = %s, s3_key = %s, landing_url = %s,
                    statements = %s, error_msg = %s, updated_at = NOW()
                WHERE run_id = %s AND agent = %s
            """, (status, pdf_url, s3_key, landing_url, statements, error_msg, run_id, agent))

def get_run(run_id: str) -> Optional[dict]:
    """Return run dict."""
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM runs WHERE run_id = %s", (run_id,))
            row = cur.fetchone()
            return dict(row) if row else None

def get_results(run_id: str) -> list[dict]:
    """Return list of result dicts."""
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM results WHERE run_id = %s ORDER BY id
            """, (run_id,))
            return [dict(row) for row in cur.fetchall()]

def get_run_counts(run_id: str) -> dict:
    """Return {total, found, not_found, pending}."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 
                    COUNT(*) as total,
                    SUM(CASE WHEN status = 'found' THEN 1 ELSE 0 END) as found,
                    SUM(CASE WHEN status = 'not_found' THEN 1 ELSE 0 END) as not_found,
                    SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending,
                    SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) as running
                FROM results WHERE run_id = %s
            """, (run_id,))
            row = cur.fetchone()
            return {
                "total": row[0] or 0,
                "found": row[1] or 0,
                "not_found": row[2] or 0,
                "pending": row[3] or 0,
                "running": row[4] or 0
            }


def get_active_work_counts() -> dict:
    """Return pending/running result counts across all runs."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending,
                    SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) as running
                FROM results
            """)
            row = cur.fetchone()
            return {
                "pending": row[0] or 0,
                "running": row[1] or 0,
            }

def stop_run(run_id: str):
    """Mark run as stopped and reset all pending/running rows to not_found."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE results
                SET status = 'not_found', updated_at = NOW()
                WHERE run_id = %s AND status IN ('pending', 'running')
            """, (run_id,))
            cur.execute("""
                UPDATE runs SET status = 'stopped', finished_at = NOW()
                WHERE run_id = %s
            """, (run_id,))

def mark_run_complete(run_id: str) -> bool:
    """Mark run as complete. Returns True only if this call was the one that changed status."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    SUM(CASE WHEN status = 'found' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN status = 'not_found' THEN 1 ELSE 0 END)
                FROM results WHERE run_id = %s
            """, (run_id,))
            row = cur.fetchone()
            found = row[0] or 0
            not_found = row[1] or 0
            cur.execute("""
                UPDATE runs
                SET status = 'complete', finished_at = NOW(),
                    found = %s, not_found = %s
                WHERE run_id = %s AND status NOT IN ('complete', 'stopped')
            """, (found, not_found, run_id))
            return cur.rowcount > 0

def claim_completion_email(run_id: str) -> bool:
    """Claim completion-email delivery for a complete run exactly once."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE runs
                SET completion_email_claimed_at = NOW()
                WHERE run_id = %s
                  AND status = 'complete'
                  AND completion_email_sent_at IS NULL
                  AND (
                      completion_email_claimed_at IS NULL
                      OR completion_email_claimed_at < NOW() - (%s || ' minutes')::interval
                  )
            """, (run_id, COMPLETION_EMAIL_CLAIM_TTL_MINUTES))
            return cur.rowcount > 0

def mark_completion_email_sent(run_id: str):
    """Persist successful completion-email delivery."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE runs
                SET completion_email_sent_at = NOW()
                WHERE run_id = %s
            """, (run_id,))

def release_completion_email_claim(run_id: str):
    """Release a failed completion-email claim so a later retry can resend."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE runs
                SET completion_email_claimed_at = NULL
                WHERE run_id = %s AND completion_email_sent_at IS NULL
            """, (run_id,))

def hash_exists(hash_hex: str) -> bool:
    """Check if hash exists."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pdf_hashes WHERE hash = %s", (hash_hex,))
            return cur.fetchone() is not None

def save_hash(hash_hex: str, agent: str, pdf_url: str, run_id: str):
    """Save PDF hash."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO pdf_hashes (hash, agent, pdf_url, run_id)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (hash) DO NOTHING
            """, (hash_hex, agent, pdf_url, run_id))

def save_landingpage(run_id: str, agent: str, country: str, landing_url: str, 
                     sub_url: str, url_type: str, confidence: str, reachable: str):
    """Save landing page result."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO landingpagedb 
                (run_id, agent, country, landing_url, sub_url, url_type, confidence, reachable)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (run_id, agent, country, landing_url, sub_url, url_type, confidence, reachable))

def clear_landingpage(run_id: str):
    """Clear landing page results for a run."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM landingpagedb WHERE run_id = %s", (run_id,))

def get_landingpage_results(run_id: str) -> list[dict]:
    """Return all landingpage results for run."""
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM landingpagedb WHERE run_id = %s ORDER BY id
            """, (run_id,))
            return [dict(row) for row in cur.fetchall()]

def set_run_lp_total(run_id: str, total: int):
    """Set the total number of landing page jobs for a run."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE runs SET lp_total = %s, lp_done = 0 WHERE run_id = %s
            """, (total, run_id))

def increment_lp_done(run_id: str) -> tuple[int, int]:
    """Atomically increment lp_done; return (lp_done, lp_total)."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE runs SET lp_done = lp_done + 1 WHERE run_id = %s
                RETURNING lp_done, lp_total
            """, (run_id,))
            row = cur.fetchone()
            return (row[0] or 0, row[1] or 0) if row else (0, 0)
