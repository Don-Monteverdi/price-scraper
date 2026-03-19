#!/usr/bin/env python3
"""
Price Intelligence Pipeline — CLI entrypoint.

Discovers all products from a client webshop (via JSON-LD schema.org),
searches price aggregators by EAN/GTIN across all competitor stores in the country,
and writes full price comparison results to a Google Sheet.

Usage:
    python3 price_scraper.py --webshop https://www.example.com --sheet <ID> --country HU
    python3 price_scraper.py --sheet <ID> --country HU --workers 10
    python3 price_scraper.py --webshop <URL> --sheet <ID> --dry-run --limit 10
"""

import argparse
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# Add parent dir to path for relative imports
sys.path.insert(0, str(Path(__file__).parent))

from scrapers.client_webshop import discover_products
from pipeline.job_queue import (
    WriteQueue, init_db, load_eans, reset_stale_jobs,
    reset_all_for_refresh, reset_stale_by_age, get_pending_jobs, get_stats,
)
from pipeline.sheet_sync import (
    ensure_tabs_exist, read_products_sheet, upsert_products_tab,
    batch_write_results,
)
from pipeline.worker import run_workers, start_writer_thread

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent / "config" / "sites.json"
DEFAULT_DB_PATH = ".price-scraper.db"
BATCH_WRITE_EVERY = 50  # write Sheet results every N completions


def load_country_config(country: str) -> dict:
    """Load country config from sites.json."""
    with open(CONFIG_PATH) as f:
        all_configs = json.load(f)
    if country not in all_configs:
        raise ValueError(
            f"Unknown country code '{country}'. "
            f"Available: {', '.join(all_configs.keys())}"
        )
    return all_configs[country]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Price Intelligence Pipeline — discover & compare webshop prices",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--webshop", metavar="URL",
                        help="Client webshop URL to discover products from (optional)")
    parser.add_argument("--sheet", metavar="SHEET_ID", required=True,
                        help="Google Sheet ID for input/output")
    parser.add_argument("--country", default="HU", metavar="CODE",
                        help="Country code: HU, DE, AT, etc. (default: HU)")
    parser.add_argument("--workers", type=int, default=10, metavar="N",
                        help="Parallel workers (default: 10)")
    parser.add_argument("--max-age-hours", type=int, default=24, metavar="N",
                        help="Re-scrape EANs older than N hours (default: 24)")
    parser.add_argument("--force-refresh", action="store_true",
                        help="Reset all jobs to pending, ignoring freshness window")
    parser.add_argument("--dry-run", action="store_true",
                        help="No writes anywhere — show what would happen and exit")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="Process only first N products (for testing)")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH, metavar="PATH",
                        help=f"SQLite DB path (default: {DEFAULT_DB_PATH})")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Enable debug logging")
    return parser.parse_args()


def main():
    args = parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # ── Load country config ──────────────────────────────────────────────────
    try:
        country_config = load_country_config(args.country)
    except (ValueError, FileNotFoundError) as e:
        logger.error(f"Config error: {e}")
        sys.exit(1)

    logger.info(f"Country: {args.country} | Aggregators: {country_config.get('aggregators')}")

    # ── Mode 1: Webshop discovery ────────────────────────────────────────────
    if args.webshop:
        logger.info(f"=== Phase 1: Product Discovery — {args.webshop} ===")
        products = discover_products(
            args.webshop,
            max_products=args.limit,
            rate_limit_seconds=country_config.get("rate_limit_seconds", 0.5),
        )

        logger.info(f"Discovered {len(products)} products with EANs")

        if not products:
            logger.warning("No products found via JSON-LD. Check: "
                           "1) URL is a product webshop, "
                           "2) Site uses schema.org/Product markup, "
                           "3) Try --verbose for details")
            sys.exit(0)

        if args.dry_run:
            logger.info("=== DRY RUN: Discovery Results (no writes) ===")
            for p in products[:20]:
                print(f"  EAN: {p['ean']} | {p['product_name'][:50]} | "
                      f"{p['client_price']} {p['client_currency']}")
            if len(products) > 20:
                print(f"  ... and {len(products) - 20} more")
            print(f"\nTotal: {len(products)} products")
            print(f"Country: {args.country} | Aggregators: {country_config.get('aggregators')}")
            print(f"Sheet: {args.sheet}")
            print("\nWould scrape prices from aggregators for each product.")
            print("Re-run without --dry-run to start.")
            sys.exit(0)

        # Write discovered products to Google Sheet
        logger.info("Writing discovered products to Google Sheet...")
        ensure_tabs_exist(args.sheet)
        upsert_products_tab(args.sheet, products)

    else:
        # No webshop — use existing Sheet data
        if args.dry_run:
            logger.info("=== DRY RUN: Reading existing Sheet (no writes) ===")
            products = read_products_sheet(args.sheet)
            if args.limit:
                products = products[:args.limit]
            print(f"Found {len(products)} products in sheet")
            print(f"Country: {args.country} | Aggregators: {country_config.get('aggregators')}")
            print(f"Would process {len(products)} EANs with {args.workers} workers")
            sys.exit(0)

        products = read_products_sheet(args.sheet)
        if args.limit:
            products = products[:args.limit]

    if not products:
        logger.error("No products to process. Use --webshop to discover products, "
                     "or ensure the Sheet has data in the Products tab.")
        sys.exit(1)

    # ── Phase 2: Price comparison pipeline ───────────────────────────────────
    logger.info(f"=== Phase 2: Price Comparison — {len(products)} products ===")

    # Init SQLite job queue
    conn = init_db(args.db_path)

    # Crash recovery: reset stale in_progress jobs
    reset_count = reset_stale_jobs(conn)
    if reset_count > 0:
        logger.info(f"Crash recovery: reset {reset_count} stale in_progress jobs to pending")

    # Load EANs into queue (INSERT OR IGNORE — skip already-queued)
    new_jobs = load_eans(conn, products)
    logger.info(f"Job queue: {new_jobs} new EANs added")

    # Apply freshness window / force refresh
    if args.force_refresh:
        refreshed = reset_all_for_refresh(conn)
        logger.info(f"Force refresh: reset {refreshed} jobs to pending")
    else:
        stale = reset_stale_by_age(conn, args.max_age_hours)
        if stale > 0:
            logger.info(f"Freshness: reset {stale} jobs older than {args.max_age_hours}h to pending")

    # Get pending jobs
    stats_before = get_stats(conn)
    pending_count = stats_before.get("pending", 0)
    logger.info(f"Jobs: {pending_count} pending | "
                f"{stats_before.get('done', 0)} done | "
                f"{stats_before.get('failed', 0)} failed")

    if pending_count == 0:
        logger.info("All jobs already done within freshness window. "
                    "Use --force-refresh to re-scrape everything.")
        sys.exit(0)

    # ── Start writer thread ──────────────────────────────────────────────────
    write_queue = WriteQueue()
    writer = start_writer_thread(conn, write_queue)

    # ── Process in batches (write Sheet every BATCH_WRITE_EVERY completions) ─
    start_time = time.time()
    total_written = 0
    written_eans: set = set()  # PERF-D: track which EANs already flushed to output

    try:
        while True:
            batch = get_pending_jobs(conn, limit=BATCH_WRITE_EVERY * args.workers)
            if not batch:
                break

            run_workers(
                batch,
                country_config,
                write_queue,
                n_workers=args.workers,
                dry_run=args.dry_run,
            )

            # Wait for writer to flush
            time.sleep(1)
            write_queue.flush(conn)

            # PERF-D: Only fetch done rows not yet written to output
            done_rows = conn.execute(
                "SELECT ean, product_name, client_price, client_currency, client_url, "
                "results_json FROM jobs WHERE status='done' AND completed_at IS NOT NULL"
            ).fetchall()

            sheet_batch = []
            for row in done_rows:
                if row["ean"] in written_eans:
                    continue
                try:
                    results = json.loads(row["results_json"] or "[]")
                    if results:
                        sheet_batch.append(results[0])
                        written_eans.add(row["ean"])
                except Exception:
                    pass

            if sheet_batch:
                batch_write_results(args.sheet, sheet_batch, batch_size=BATCH_WRITE_EVERY)
                total_written += len(sheet_batch)

            # Progress check
            stats = get_stats(conn)
            remaining = stats.get("pending", 0)
            if remaining == 0:
                break

    except KeyboardInterrupt:
        logger.info("\nInterrupted. Progress saved — re-run to continue from where you left off.")
    finally:
        # Stop writer thread gracefully
        write_queue.stop()
        writer.join(timeout=5)
        write_queue.flush(conn)

    # ── Final stats ──────────────────────────────────────────────────────────
    elapsed = time.time() - start_time
    final_stats = get_stats(conn)
    total = final_stats.get("total", 0)
    done = final_stats.get("done", 0)
    failed = final_stats.get("failed", 0)
    blocked = final_stats.get("blocked", 0)
    coverage = round(done / total * 100, 1) if total > 0 else 0

    print("\n" + "═" * 50)
    print("  Price Scrape Complete")
    print("═" * 50)
    print(f"  Total products:    {total}")
    print(f"  Coverage:          {done} ({coverage}%)")
    print(f"  Failed:            {failed}")
    print(f"  Blocked:           {blocked}")
    print(f"  Sheet written:     {total_written} rows")
    print(f"  Elapsed:           {elapsed:.0f}s")
    print(f"  Sheet ID:          {args.sheet}")
    print("═" * 50)

    # Output machine-readable summary (for agent output contract validation)
    summary = {
        "total_products": total,
        "products_with_results": done,
        "coverage_percent": coverage,
        "failed": failed,
        "blocked": blocked,
        "sheet_id": args.sheet,
        "country": args.country,
        "scraped_at": datetime.utcnow().isoformat(),
    }
    print(f"\nSUMMARY_JSON: {json.dumps(summary)}")

    if failed > 0 or blocked > 0:
        logger.info(f"Re-run to retry {failed} failed + {blocked} blocked jobs")

    conn.close()


if __name__ == "__main__":
    main()
