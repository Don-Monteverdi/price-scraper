# Architecture

## Module Dependency Graph

```
price_scraper.py  (CLI orchestration)
  ├── scrapers/client_webshop.py   (product discovery)      ← no inter-scraper deps
  ├── pipeline/job_queue.py        (SQLite WAL queue)        ← no scraper deps
  ├── pipeline/sheet_sync.py       (JSON output)             ← no scraper deps
  └── pipeline/worker.py           (ThreadPool workers)
        ├── scrapers/aggregator.py (arukereso/Google/idealo)
        └── scrapers/direct.py     (competitor fallback)

scrapers/utils.py  (shared utilities — imported by all scrapers + pipeline)
```

All scrapers import shared utilities from `scrapers/utils.py` — never from each other.

## Design Decisions

### SQLite Job Queue (not Redis, not Celery)

**Why:** The pipeline runs as a single CLI process with 10 threads. SQLite in WAL mode handles this perfectly with zero infrastructure. Job state survives crashes (the DB is on disk). No need for a separate queue service.

**Thread model:** Workers push results to a `queue.Queue` (Python stdlib). A single dedicated writer thread drains it and writes to SQLite. This eliminates lock contention entirely.

### Local JSON Output (not direct Google Sheets API)

**Why:** The scraper runs as a standalone CLI tool or as an agent subagent. In agent mode, the calling agent already has access to Google Workspace MCP tools. Writing to local JSON files decouples the scraper from any specific Sheets API client, making it testable and portable.

The calling agent reads `output/products.json` and `output/all_prices.json` and writes to Google Sheets using whatever API client it has.

### Aggregator Cascade (not parallel)

**Why:** Within each worker, aggregators are searched sequentially because:
1. arukereso.hu covers 400+ stores in one request — often sufficient alone
2. Google Shopping requires a browser subprocess — expensive to run unnecessarily
3. Direct competitor scraping is a last resort for sparse results

The cascade short-circuits: if arukereso.hu returns enough results, Google Shopping and direct scraping are skipped.

### Product Name Search (not just EAN)

**Why:** arukereso.hu (the primary HU aggregator) does not support EAN search. It requires product name search with fuzzy matching. The pipeline uses token-sorted `SequenceMatcher` similarity scoring with a 0.45 reliability threshold. Below this threshold, the delta calculation is flagged as unreliable.

## Concurrency Model

```
Main Thread
    │
    ├── discover_products()           ← sequential (rate-limited)
    │
    ├── load_eans(conn, products)     ← batch INSERT (executemany)
    │
    └── while pending:
          │
          ├── get_pending_jobs()      ← atomic (BEGIN IMMEDIATE)
          │
          ├── ThreadPoolExecutor(N)
          │     ├── Worker 1 ─→ process_ean() ─→ WriteQueue.push()
          │     ├── Worker 2 ─→ process_ean() ─→ WriteQueue.push()
          │     └── Worker N ─→ process_ean() ─→ WriteQueue.push()
          │
          ├── Writer Thread           ← drains queue, writes SQLite
          │
          └── batch_write_results()   ← writes JSON output files
```

### Rate Limiting

One `RateLimiter` instance is shared across all workers. It uses a `threading.Lock` to ensure that even with 10 concurrent threads, requests to the same domain are spaced by the configured delay. This prevents IP bans from aggregator sites.

## File Responsibilities

| File | LOC | Responsibility |
|------|-----|----------------|
| `price_scraper.py` | ~300 | CLI parsing, orchestration, progress reporting |
| `scrapers/utils.py` | ~120 | Price parsing, rate limiting, dedup, similarity, shared headers |
| `scrapers/client_webshop.py` | ~380 | Webshop crawling, JSON-LD extraction, CSS heuristics |
| `scrapers/aggregator.py` | ~510 | arukereso.hu, Google Shopping, idealo scrapers |
| `scrapers/direct.py` | ~300 | Direct competitor EAN + name search |
| `pipeline/job_queue.py` | ~230 | SQLite schema, job lifecycle, WriteQueue |
| `pipeline/worker.py` | ~170 | ThreadPool management, worker logic |
| `pipeline/sheet_sync.py` | ~240 | JSON file I/O, delta calculation |
| `config/sites.json` | ~80 | Country configuration |

## Security Model

```
Untrusted input:
  ├── --webshop URL          → _assert_safe_url() blocks private IPs
  ├── JSON-LD from pages     → size-bounded, item-count-bounded
  ├── href from aggregators  → scheme-validated (http/https only)
  ├── competitor domains     → _validate_domain() rejects IPs and injection
  └── EAN values             → URL-encoded before use in URLs

Trusted input:
  ├── config/sites.json      → static, checked into git
  └── --sheet ID             → opaque identifier, no injection surface
```
