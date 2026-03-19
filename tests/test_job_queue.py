"""Tests for job_queue — atomic claims, crash recovery, empty list guard."""
import sqlite3
import threading
import tempfile
import os

import pytest

from pipeline.job_queue import (
    init_db, load_eans, get_pending_jobs, reset_stale_jobs, get_stats,
    STATUS_PENDING, STATUS_IN_PROGRESS, STATUS_DONE,
)


@pytest.fixture
def db_conn():
    """Create a temporary SQLite DB for testing."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = init_db(path)
    yield conn
    conn.close()
    os.unlink(path)


def _sample_products(n: int) -> list[dict]:
    return [
        {
            "ean": f"EAN{i:04d}",
            "product_name": f"Product {i}",
            "client_price": 100.0 + i,
            "client_currency": "HUF",
            "client_url": f"https://example.com/p/{i}",
        }
        for i in range(n)
    ]


def test_load_eans_batch(db_conn):
    """Batch insert should add all products."""
    products = _sample_products(50)
    inserted = load_eans(db_conn, products)
    assert inserted == 50
    # Re-insert should skip all (INSERT OR IGNORE)
    inserted2 = load_eans(db_conn, products)
    assert inserted2 == 0


def test_load_eans_empty(db_conn):
    """Empty product list should not crash."""
    assert load_eans(db_conn, []) == 0


def test_get_pending_jobs_atomic(db_conn):
    """Sequential claims should get no duplicates and drain all jobs."""
    load_eans(db_conn, _sample_products(20))

    # First claim: get up to 15
    batch1 = get_pending_jobs(db_conn, limit=15)
    # Second claim: get remaining
    batch2 = get_pending_jobs(db_conn, limit=15)

    all_eans = [j["ean"] for j in batch1] + [j["ean"] for j in batch2]

    # No duplicates
    assert len(all_eans) == len(set(all_eans)), "Duplicate EANs claimed!"
    # Total should be exactly 20
    assert len(all_eans) == 20
    # First batch should be 15 (the limit)
    assert len(batch1) == 15
    # Second batch should be 5 (remaining)
    assert len(batch2) == 5


def test_crash_recovery(db_conn):
    """Stale in_progress jobs should be reset to pending."""
    load_eans(db_conn, _sample_products(5))
    # Claim all 5
    get_pending_jobs(db_conn, limit=5)

    # Simulate stale: set last_attempted_at to long ago
    db_conn.execute(
        "UPDATE jobs SET last_attempted_at = datetime('now', '-10 minutes') "
        "WHERE status = ?",
        (STATUS_IN_PROGRESS,),
    )
    db_conn.commit()

    reset_count = reset_stale_jobs(db_conn)
    assert reset_count == 5

    stats = get_stats(db_conn)
    assert stats.get(STATUS_PENDING, 0) == 5


def test_get_stats_total(db_conn):
    """Stats total should be derived from sum, not a separate query."""
    load_eans(db_conn, _sample_products(10))
    stats = get_stats(db_conn)
    assert stats["total"] == 10
    assert stats.get(STATUS_PENDING, 0) == 10


def test_empty_ean_list_guard(db_conn):
    """get_pending_jobs with no pending rows should return empty list."""
    result = get_pending_jobs(db_conn, limit=10)
    assert result == []
