"""
Parallel worker pool for EAN price comparison.
Uses ThreadPoolExecutor with a single-writer thread to avoid SQLite lock contention.
Workers push results to a WriteQueue; a dedicated thread persists to SQLite.
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from scrapers.utils import RateLimiter, dedupe_results
from scrapers.aggregator import search_aggregators
from scrapers.direct import search_all_competitors, search_all_competitors_by_name
from pipeline.job_queue import WriteQueue, MAX_RETRIES

logger = logging.getLogger(__name__)

# Threshold below which we also run direct competitor scraping
SPARSE_RESULTS_THRESHOLD = 2


def process_ean(
    ean: str,
    product_name: str,
    client_price: float,
    client_currency: str,
    client_url: str,
    country_config: dict,
    write_queue: WriteQueue,
    rate_limiter: RateLimiter,
    dry_run: bool = False,
    attempts: int = 0,
) -> str:
    """
    Process a single EAN: search aggregators, fallback to direct, push result.
    Designed to be called from a ThreadPoolExecutor worker.
    """
    if dry_run:
        logger.info(f"[dry-run] Would process EAN {ean} ({product_name})")
        return "done"

    try:
        # Step 1: Search aggregators (product_name required for HU/arukereso.hu)
        results = search_aggregators(ean, country_config, rate_limiter, product_name=product_name)

        # Step 2: If sparse results, also try direct competitor scraping
        competitor_domains = country_config.get("competitors", [])
        if len(results) < SPARSE_RESULTS_THRESHOLD and competitor_domains:
            # ARCH-E: Route name-based search when EAN is empty
            if not ean and product_name:
                direct_results = search_all_competitors_by_name(
                    product_name, competitor_domains, rate_limiter
                )
            else:
                direct_results = search_all_competitors(ean, competitor_domains, rate_limiter)
            results.extend(direct_results)
            # Re-sort by price
            results.sort(key=lambda x: x.get("price") or float("inf"))

        # Step 3: Dedupe by (store_name, price)
        results = dedupe_results(results)

        # Build structured result
        cheapest = results[0] if results else {}
        # match_reliable is False if ANY result flagged a poor product match
        match_reliable = all(r.get("match_reliable", True) for r in results) if results else True
        structured = {
            "ean": ean,
            "product_name": product_name,
            "client_price": client_price,
            "client_currency": client_currency,
            "client_url": client_url,
            "cheapest_price": cheapest.get("price"),
            "cheapest_store": cheapest.get("store_name", ""),
            "cheapest_currency": cheapest.get("currency", ""),
            "stores_count": len(results),
            "match_reliable": match_reliable,
            "all_prices": results,
        }

        write_queue.push_done(ean, [structured])
        logger.debug(f"EAN {ean}: {len(results)} stores found, cheapest = {cheapest.get('price')} {cheapest.get('currency')}")
        return "done"

    except Exception as e:
        logger.warning(f"EAN {ean} failed (attempt {attempts + 1}): {e}")
        if _is_blocked_error(e):
            write_queue.push_blocked(ean, str(e), attempts + 1)
            return "blocked"
        else:
            write_queue.push_failed(ean, str(e), attempts + 1)
            return "failed"


def _is_blocked_error(exc: Exception) -> bool:
    """Detect bot-blocking errors (403, CAPTCHA) vs transient failures."""
    msg = str(exc).lower()
    return any(keyword in msg for keyword in ["403", "captcha", "cloudflare", "access denied", "blocked"])


def run_workers(
    jobs: list[dict],
    country_config: dict,
    write_queue: WriteQueue,
    n_workers: int = 10,
    dry_run: bool = False,
) -> dict:
    """
    Process all jobs in parallel using ThreadPoolExecutor.
    Returns summary stats: {total, done, failed, blocked}.
    """
    if not jobs:
        logger.info("No pending jobs to process")
        return {"total": 0, "done": 0, "failed": 0, "blocked": 0}

    rate_limiter = RateLimiter(delay_seconds=country_config.get("rate_limit_seconds", 2))
    stats = {"total": len(jobs), "done": 0, "failed": 0, "blocked": 0}

    logger.info(f"Starting {n_workers} workers for {len(jobs)} EANs")

    try:
        from tqdm import tqdm
        progress = tqdm(total=len(jobs), unit="EAN", desc="Scraping prices")
    except ImportError:
        progress = None

    with ThreadPoolExecutor(max_workers=n_workers) as executor:
        futures = {
            executor.submit(
                process_ean,
                job["ean"],
                job.get("product_name", ""),
                job.get("client_price"),
                job.get("client_currency", ""),
                job.get("client_url", ""),
                country_config,
                write_queue,
                rate_limiter,
                dry_run,
                job.get("attempts", 0),
            ): job["ean"]
            for job in jobs
        }

        for future in as_completed(futures):
            ean = futures[future]
            try:
                status = future.result()
                if status == "blocked":
                    stats["blocked"] += 1
                elif status == "failed":
                    stats["failed"] += 1
                else:
                    stats["done"] += 1
            except Exception as e:
                logger.error(f"Unhandled exception for EAN {ean}: {e}")
                stats["failed"] += 1

            if progress:
                progress.update(1)

    if progress:
        progress.close()

    # Drain remaining write queue items
    time.sleep(0.5)

    logger.info(
        f"Workers complete: {stats['done']} done, "
        f"{stats['failed']} failed, {stats['blocked']} blocked"
    )
    return stats


def start_writer_thread(conn, write_queue: WriteQueue) -> threading.Thread:
    """
    Start the dedicated SQLite writer thread.
    Returns the thread (already started). Call write_queue.stop() to terminate.
    """
    from pipeline.job_queue import writer_thread
    t = threading.Thread(
        target=writer_thread,
        args=(conn, write_queue),
        daemon=True,
        name="sqlite-writer",
    )
    t.start()
    return t
