"""
SQLite job queue for price-scraper pipeline.
WAL mode + single-writer thread pattern. Crash-safe: stale in_progress jobs
are reset to pending on every startup.
"""

import sqlite3
import json
import queue
import threading
import time
from datetime import datetime
from pathlib import Path


CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
    ean TEXT PRIMARY KEY,
    product_name TEXT,
    client_price REAL,
    client_currency TEXT,
    client_url TEXT,
    status TEXT DEFAULT 'pending',
    attempts INTEGER DEFAULT 0,
    last_error TEXT,
    last_attempted_at TEXT,
    completed_at TEXT,
    results_json TEXT
);
"""

STATUS_PENDING = "pending"
STATUS_IN_PROGRESS = "in_progress"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_BLOCKED = "blocked"

MAX_RETRIES = 3
STALE_MINUTES = 5


def init_db(db_path: str) -> sqlite3.Connection:
    """Create/open the SQLite DB with WAL mode enabled."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(CREATE_TABLE_SQL)
    conn.commit()
    return conn


def reset_stale_jobs(conn: sqlite3.Connection) -> int:
    """Reset in_progress jobs older than STALE_MINUTES to pending (crash recovery)."""
    cur = conn.execute(
        """
        UPDATE jobs
        SET status = ?, last_error = 'reset: stale in_progress'
        WHERE status = ?
          AND last_attempted_at < datetime('now', ?)
        """,
        (STATUS_PENDING, STATUS_IN_PROGRESS, f"-{STALE_MINUTES} minutes"),
    )
    conn.commit()
    return cur.rowcount


def load_eans(conn: sqlite3.Connection, products: list[dict]) -> int:
    """
    Insert products into job queue (INSERT OR IGNORE — skip existing EANs).
    products: list of {ean, product_name, client_price, client_currency, client_url}
    Returns count of newly inserted rows.
    """
    if not products:
        return 0
    rows_before = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    conn.executemany(
        """
        INSERT OR IGNORE INTO jobs
            (ean, product_name, client_price, client_currency, client_url)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            (
                p.get("ean"),
                p.get("product_name", ""),
                p.get("client_price"),
                p.get("client_currency", ""),
                p.get("client_url", ""),
            )
            for p in products
        ],
    )
    conn.commit()
    rows_after = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    return rows_after - rows_before


def reset_all_for_refresh(conn: sqlite3.Connection) -> int:
    """Force-reset all done/failed/blocked jobs to pending (--force-refresh)."""
    cur = conn.execute(
        "UPDATE jobs SET status = ?, completed_at = NULL WHERE status != ?",
        (STATUS_PENDING, STATUS_IN_PROGRESS),
    )
    conn.commit()
    return cur.rowcount


def reset_stale_by_age(conn: sqlite3.Connection, max_age_hours: int) -> int:
    """Reset done jobs older than max_age_hours back to pending (freshness window)."""
    cur = conn.execute(
        """
        UPDATE jobs
        SET status = ?, completed_at = NULL
        WHERE status = ?
          AND completed_at < datetime('now', ?)
        """,
        (STATUS_PENDING, STATUS_DONE, f"-{max_age_hours} hours"),
    )
    conn.commit()
    return cur.rowcount


def get_pending_jobs(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    """
    Fetch up to `limit` pending jobs and mark them in_progress atomically.
    Returns list of job dicts.
    """
    now = datetime.utcnow().isoformat()
    # ARCH-B: Atomic SELECT + UPDATE using a single UPDATE ... RETURNING pattern
    # SQLite 3.35+ supports RETURNING, but for broader compat use subquery UPDATE
    rows = conn.execute(
        """
        UPDATE jobs
        SET status = ?, last_attempted_at = ?
        WHERE ean IN (
            SELECT ean FROM jobs WHERE status = ? ORDER BY rowid LIMIT ?
        )
        """,
        (STATUS_IN_PROGRESS, now, STATUS_PENDING, limit),
    )
    conn.commit()

    # Now fetch the rows we just claimed
    claimed = conn.execute(
        "SELECT ean, product_name, client_price, client_currency, client_url "
        "FROM jobs WHERE status = ? AND last_attempted_at = ?",
        (STATUS_IN_PROGRESS, now),
    ).fetchall()

    return [dict(r) for r in claimed]


def get_stats(conn: sqlite3.Connection) -> dict:
    """Return job status counts."""
    rows = conn.execute(
        "SELECT status, COUNT(*) as cnt FROM jobs GROUP BY status"
    ).fetchall()
    stats = {r["status"]: r["cnt"] for r in rows}
    stats["total"] = sum(stats.values())  # PERF-J: derive from GROUP BY, no second query
    return stats


# ── Write queue (single-writer pattern) ──────────────────────────────────────

class WriteQueue:
    """
    Thread-safe queue for worker results.
    Workers push dicts here; a single writer thread drains and persists to SQLite.
    This avoids concurrent writes and SQLite lock contention.
    """

    def __init__(self):
        self._q = queue.Queue()
        self._stop = threading.Event()

    def push_done(self, ean: str, results: list[dict]):
        self._q.put({"ean": ean, "status": STATUS_DONE, "results": results})

    def push_failed(self, ean: str, error: str, attempts: int):
        new_status = STATUS_FAILED if attempts >= MAX_RETRIES else STATUS_PENDING
        self._q.put({"ean": ean, "status": new_status, "error": error, "attempts": attempts})

    def push_blocked(self, ean: str, error: str, attempts: int):
        self._q.put({"ean": ean, "status": STATUS_BLOCKED, "error": error, "attempts": attempts})

    def stop(self):
        self._stop.set()

    def flush(self, conn: sqlite3.Connection, timeout: float = 0.1) -> int:
        """Drain queue and persist to SQLite. Returns number of rows written."""
        written = 0
        now = datetime.utcnow().isoformat()
        batch = []

        while True:
            try:
                item = self._q.get(timeout=timeout)
                batch.append(item)
                self._q.task_done()
            except queue.Empty:
                break

        for item in batch:
            ean = item["ean"]
            status = item["status"]

            if status == STATUS_DONE:
                conn.execute(
                    "UPDATE jobs SET status=?, results_json=?, completed_at=?, last_error=NULL "
                    "WHERE ean=?",
                    (STATUS_DONE, json.dumps(item["results"]), now, ean),
                )
            else:
                conn.execute(
                    "UPDATE jobs SET status=?, last_error=?, attempts=?, last_attempted_at=? "
                    "WHERE ean=?",
                    (status, item.get("error", ""), item.get("attempts", 0), now, ean),
                )
            written += 1

        if batch:
            conn.commit()

        return written


def writer_thread(conn: sqlite3.Connection, wq: WriteQueue):
    """
    Dedicated writer thread. Drains the WriteQueue and persists to SQLite.
    Run in a background thread via threading.Thread(target=writer_thread, ...).
    """
    while not wq._stop.is_set():
        wq.flush(conn, timeout=0.5)
        time.sleep(0.1)
    # Final drain after stop signal
    wq.flush(conn, timeout=0.1)
