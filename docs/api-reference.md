# API Reference

## Core Modules

### `scrapers/utils.py` — Shared Utilities

#### `parse_price(text: str) -> Optional[float]`
Parse a price string to float. Handles European (`1.234,56`), US (`1,234.56`), Hungarian thousands (`1.234` = 1234), and mixed formats. Returns `None` for unparseable input.

#### `name_similarity(a: str, b: str) -> float`
Case-insensitive, token-order-independent similarity score (0.0-1.0) using `SequenceMatcher`. Used to match product names across different stores.

#### `dedupe_results(results: list[dict]) -> list[dict]`
Deduplicate price results by `(store_name, price)` key. Keeps first occurrence. Returns sorted by price ascending.

#### `extract_base_url(url: str) -> str`
Extract `scheme://netloc` from a URL. Returns `""` for relative URLs.

#### `class RateLimiter(delay_seconds: float = 2.0)`
Thread-safe per-domain rate limiter. Call `rate_limiter.wait("domain.com")` before each request.

---

### `scrapers/client_webshop.py` — Product Discovery

#### `discover_products(webshop_url, max_pages=5000, max_products=None, rate_limit_seconds=0.5) -> list[dict]`
Discover all products from a webshop URL. Returns list of `{ean, product_name, client_price, client_currency, client_url}`.

1. Tries `sitemap.xml` first
2. Falls back to BFS crawl
3. Extracts product data via JSON-LD, then CSS heuristics

#### `extract_product_from_page(url: str) -> Optional[dict]`
Extract product data from a single product page URL.

---

### `scrapers/aggregator.py` — Price Aggregators

#### `search_aggregators(ean, country_config, rate_limiter, product_name="") -> list[dict]`
Search all configured aggregators for a product. Returns deduplicated results sorted by price.

#### `search_arhu(ean="", rate_limiter=None, product_name="") -> list[dict]`
Search arukereso.hu by product name. Follows aggregate "N ajánlat" pages. Returns results with `match_reliable` flag.

#### `search_google_shopping(ean, country_code, rate_limiter) -> list[dict]`
Search Google Shopping via agent-browser. Returns `[]` if agent-browser unavailable.

#### `search_idealo(ean, tld, rate_limiter) -> list[dict]`
Search idealo.{tld} by EAN.

---

### `scrapers/direct.py` — Direct Competitor Search

#### `search_competitor(ean, domain, rate_limiter) -> list[dict]`
Search a single competitor domain by EAN. Tries common search URL patterns.

#### `search_all_competitors(ean, competitor_domains, rate_limiter) -> list[dict]`
Search all competitor domains in parallel (5 workers).

#### `search_competitor_by_name(product_name, domain, rate_limiter, price_floor=0.0) -> list[dict]`
Search by product name instead of EAN. Used for products without standard barcodes.

#### `search_all_competitors_by_name(product_name, competitor_domains, rate_limiter, price_floor=0.0) -> list[dict]`
Search all competitor domains by name in parallel.

---

### `pipeline/job_queue.py` — SQLite Job Queue

#### `init_db(db_path: str) -> sqlite3.Connection`
Create/open the SQLite DB with WAL mode. Creates `jobs` table if not exists.

#### `load_eans(conn, products: list[dict]) -> int`
Batch insert products into job queue (INSERT OR IGNORE). Returns count of new rows.

#### `get_pending_jobs(conn, limit=100) -> list[dict]`
Atomically fetch and claim pending jobs. Uses `BEGIN IMMEDIATE` to prevent double-claiming.

#### `reset_stale_jobs(conn) -> int`
Reset `in_progress` jobs older than 5 minutes to `pending` (crash recovery).

#### `get_stats(conn) -> dict`
Return job status counts: `{pending, in_progress, done, failed, blocked, total}`.

#### `class WriteQueue`
Thread-safe queue for worker results. Workers call `push_done()`, `push_failed()`, or `push_blocked()`. The dedicated writer thread drains via `flush()`.

---

### `pipeline/worker.py` — Parallel Workers

#### `process_ean(ean, product_name, ...) -> str`
Process a single EAN through the aggregator cascade. Returns `"done"`, `"failed"`, or `"blocked"`.

#### `run_workers(jobs, country_config, write_queue, n_workers=10, dry_run=False) -> dict`
Process all jobs in parallel. Returns `{total, done, failed, blocked}`.

---

### `pipeline/sheet_sync.py` — Output

#### `batch_write_results(sheet_id, results, batch_size=50) -> int`
Write comparison results to `output/products.json` and `output/all_prices.json`. Returns count written.

#### `_calculate_delta(client_price, cheapest_price, client_currency, cheapest_currency, match_reliable=True) -> str`
Calculate signed price delta %. Returns `"—"` for cross-currency, unknown currency, or missing values.

## Data Structures

### Product Dict
```python
{
    "ean": str,              # EAN/GTIN barcode
    "product_name": str,     # Product name from webshop
    "client_price": float,   # Price on client's webshop
    "client_currency": str,  # ISO 4217 (e.g. "HUF")
    "client_url": str,       # Product page URL on client's site
}
```

### Price Result Dict
```python
{
    "store_name": str,       # Competitor store name
    "store_url": str,        # Store homepage URL
    "product_url": str,      # Product page on competitor site
    "price": float,          # Listed price
    "currency": str,         # ISO 4217
    "match_reliable": bool,  # True if name similarity >= 0.45
}
```

### Job Statuses
| Status | Meaning |
|--------|---------|
| `pending` | Not yet processed |
| `in_progress` | Currently being processed by a worker |
| `done` | Successfully scraped |
| `failed` | Failed after max retries (3) |
| `blocked` | Bot detection (403/CAPTCHA) |
