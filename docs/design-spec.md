# Price Intelligence Pipeline — Design Spec
**Date:** 2026-03-12
**Status:** Approved

---

## Problem

Retailers need to know how their prices compare against all competitors in their market. Manual price checking across thousands of products is not feasible. A general-purpose tool is needed that:

1. Discovers all products from a client's webshop automatically
2. Finds those same products on every other webshop in the country via EAN/GTIN barcode
3. Returns full competitor price lists with product page links
4. Handles thousands of products reliably, resumably, and without manual intervention

---

## Solution Overview

A Python CLI pipeline that:
- Crawls a client webshop to extract products via JSON-LD schema.org markup
- Populates a Google Sheet with discovered products
- For each product EAN, searches price aggregators (e.g. ár.hu, Google Shopping) for competitor prices
- Falls back to direct competitor site scraping when aggregators miss a product
- Falls back to Playwright (agent-browser) when HTTP requests are blocked
- Stores ALL competitor prices + product page URLs per EAN
- Writes results back to the Google Sheet (two tabs: Products + All Prices)
- Is resumable by default — every run skips already-done jobs

---

## Architecture

```
tools/price-scraper/
├── price_scraper.py          # CLI entrypoint (argparse, orchestration)
├── scrapers/
│   ├── client_webshop.py     # Crawl client's webshop → discover products
│   ├── aggregator.py         # Search price aggregators by EAN
│   └── direct.py             # Direct competitor site search fallback
├── pipeline/
│   ├── job_queue.py          # SQLite job queue (state management)
│   ├── worker.py             # ThreadPoolExecutor parallel worker logic
│   └── sheet_sync.py         # Google Sheets read/write (via Google Workspace MCP)
└── config/
    └── sites.json            # Country → aggregators + competitor domain list
```

Follows the subprocess/worker pattern of `tools/research-pipeline.py`.

---

## Data Flow

```
[Mode 1: --webshop provided]
Client webshop URL
  → client_webshop.py: crawl all product pages (sitemap.xml first, then recursive)
  → Extract JSON-LD Product schema: EAN, name, price, product URL
  → Upsert to Google Sheet "Products" tab (by EAN key — update if exists, insert if new)

[Mode 2: --sheet provided (or after Mode 1)]
Google Sheet "Products" tab
  → EAN list → SQLite job queue (status: pending/in_progress/done/failed/blocked)
  → On startup: reset any in_progress jobs older than 5 min to pending (crash recovery)
    → N parallel workers (ThreadPoolExecutor)
    → Workers push results to a single write queue (one writer thread) — no direct DB writes
        1. Search price aggregators (ár.hu, Google Shopping) for EAN
           → parse: store name, store URL, product page URL, price, currency
        2. If no/partial results → search known competitor domains via requests + BS4
        3. If blocked (403, CAPTCHA) → fallback to agent-browser Playwright session
        4. If agent-browser unavailable → log warning, mark job failed, continue
      → Write queue flushes to SQLite (WAL mode, single writer thread)
    → Batch upsert to Google Sheet every 50 completions:
        "Products" tab: upsert by EAN — cheapest same-currency price, store, delta %, # stores, timestamp
        "All Prices" tab: upsert by (EAN × store) key — update row if exists, insert if new
```

---

## Google Sheet Structure

### Tab 1: Products
| Column | Description |
|---|---|
| EAN | Product barcode (upsert key) |
| Product Name | From client webshop or manual |
| Client Price | Current price on client's webshop |
| Client Currency | ISO 4217 (e.g. HUF, EUR) |
| Client URL | Product page URL on client's site |
| Cheapest Price | Lowest competitor price (same currency only) |
| Cheapest Store | Name of store with lowest price |
| Delta % | Signed: positive = client charges MORE than cheapest. Formula: `(client_price - cheapest_price) / cheapest_price * 100`. "—" if cross-currency. |
| # Stores Found | Count of competitors carrying this product |
| Last Scraped | ISO timestamp of last comparison run |

**Delta % sign convention:** positive numbers are a problem (client overpriced). Negative numbers are an opportunity (client is cheaper — can raise price or use as marketing).

### Tab 2: All Prices
| Column | Description |
|---|---|
| EAN | Product barcode (upsert key part 1) |
| Product Name | For readability |
| Store Name | Competitor store name (upsert key part 2) |
| Store URL | Homepage of competitor |
| Product Page URL | Exact URL of this product on competitor site |
| Price | Listed price |
| Currency | ISO 4217 |
| Scraped At | ISO timestamp |

**Upsert key:** (EAN, Store Name) — re-runs update the row rather than duplicating.

---

## Scraper Modules

### client_webshop.py — Client Discovery

**JSON-LD field priority** (in order, first match wins):
1. `gtin13`
2. `gtin14`
3. `gtin8`
4. `gtin` (generic)
5. `sku` (fallback — note in log: "using SKU as EAN proxy")

**Discovery steps:**
1. Try `{webshop_url}/sitemap.xml` → find all product URLs (fastest path)
2. Fallback: recursive crawl from homepage following internal links, filter to product pages (URL patterns: `/product/`, `/termek/`, `/p/`, etc.)
3. For each product URL: extract `<script type="application/ld+json">` with `@type: Product`
4. If no JSON-LD: fall back to CSS selector heuristics (common price/EAN field patterns per platform)

Works for ~90% of modern e-commerce platforms (Shopify, WooCommerce, Magento, PrestaShop, OpenCart).

**Upsert behavior:** EAN already in Sheet → update name/price/URL if changed. New EAN → insert row.

### aggregator.py — Primary Source

Country-configured scrapers that search by EAN:

**ár.hu (HU):**
- URL: `https://ar.hu/kereses/?q={EAN}`
- Method: `requests` + BeautifulSoup
- Parses product card grid → store name, store URL, product URL, price, currency (HUF)
- Indexes 400+ Hungarian webshops — single request returns all of them

**Google Shopping:**
- URL: `https://www.google.com/search?q={EAN}&tbm=shop&gl={country_code}`
- Method: agent-browser (Playwright) — required due to Google bot detection
- Parses Shopping SERP → store name, product URL, price, currency
- **Dependency note:** If agent-browser is unavailable, log `WARNING: Google Shopping skipped — agent-browser not available` and continue with other sources. Countries with only Google Shopping as aggregator log a note per EAN.

**idealo (DE, AT, CH, FR, IT, ES, PL):**
- URL: `https://www.idealo.{tld}/preisvergleich/MainSearchProductCategory.html?q={EAN}`
- Method: `requests` + BeautifulSoup

### direct.py — Fallback Source

For EANs not found on aggregators:
- Use Firecrawl structured extract against known competitor product page patterns
- `extract({url}/search?q={EAN}, schema={price, name, url, currency})`
- Competitor domains configured per country in `config/sites.json`

---

## Reliability & State Management

### SQLite Job Queue (pipeline/job_queue.py)

**WAL mode:** Database opened with `PRAGMA journal_mode=WAL` for concurrent reads.
**Single writer thread:** All workers push results to a `queue.Queue`; one dedicated writer thread drains it. No direct DB writes from worker threads — eliminates lock contention.

```sql
CREATE TABLE jobs (
  ean TEXT PRIMARY KEY,
  product_name TEXT,
  status TEXT DEFAULT 'pending',  -- pending|in_progress|done|failed|blocked
  attempts INTEGER DEFAULT 0,
  last_error TEXT,
  last_attempted_at TEXT,         -- ISO timestamp
  completed_at TEXT,
  results_json TEXT               -- JSON array of all competitor results
);
```

### Crash Recovery

On every startup, before processing begins:
```sql
UPDATE jobs SET status = 'pending', last_error = 'reset: stale in_progress'
WHERE status = 'in_progress'
AND last_attempted_at < datetime('now', '-5 minutes');
```

### Behavior

| Feature | Detail |
|---|---|
| Resume | Every run is resumable by default — skips `done` jobs within freshness window. No flag needed. |
| Freshness | Configurable `--max-age-hours N` (default 24) — jobs older than N hours reset to `pending` |
| Force refresh | `--force-refresh` flag resets ALL jobs to `pending` regardless of age |
| Retries | Max 3 attempts per EAN, exponential backoff (5s, 15s, 45s) |
| Rate limiting | Per-domain configurable delay in `sites.json` (default 2s between requests to same domain) |
| Batch writes | Google Sheet updated every 50 completions — avoids Sheets API rate limits |
| Workers | `--workers N` (default 10), tune for speed vs. ban risk |

### Job Status Flow
```
pending → in_progress → done
                     ↘ failed (retried up to 3x, then stays failed)
                     ↘ blocked (requires Playwright, uses fallback)
```

---

## Configuration (config/sites.json)

```json
{
  "HU": {
    "aggregators": ["arhu", "google_shopping"],
    "competitors": ["jatekshop.hu", "pepita.hu", "konzolvilag.hu"],
    "currency": "HUF",
    "language": "hu",
    "rate_limit_seconds": 2
  },
  "DE": {
    "aggregators": ["idealo", "google_shopping"],
    "competitors": [],
    "currency": "EUR",
    "language": "de",
    "rate_limit_seconds": 2
  }
}
```

---

## CLI Interface

```bash
# Discover products from webshop + compare against all competitors
python3 tools/price-scraper/price_scraper.py \
  --webshop https://www.example.com \
  --sheet <GOOGLE_SHEET_ID> \
  --country HU \
  --workers 10

# Compare existing sheet data (skip discovery)
python3 tools/price-scraper/price_scraper.py \
  --sheet <GOOGLE_SHEET_ID> \
  --country HU \
  --workers 10

# Test with first 10 products — no writes to Sheet or SQLite
# Prints: what would be discovered, which scrapers would run, sample output
python3 tools/price-scraper/price_scraper.py \
  --webshop https://www.example.com \
  --sheet <SHEET_ID> \
  --country HU \
  --dry-run \
  --limit 10

# Force re-scrape all (ignore freshness window)
python3 tools/price-scraper/price_scraper.py \
  --sheet <SHEET_ID> \
  --country HU \
  --force-refresh
```

**`--dry-run` behavior:** No writes anywhere (Sheet or SQLite). Runs discovery and prints discovered products + which scrapers would be invoked. Exits before any price scraping. Used for testing webshop discovery configuration.

---

## Currency Handling

- Each price result carries a `currency` field (ISO 4217)
- Delta % is only calculated when `client_currency == competitor_currency`
- Cross-currency results are stored in All Prices tab with their original currency
- Delta % column shows "—" with a note "cross-currency" when currencies differ
- No FX conversion in v1 (avoid stale exchange rates)

---

## Error Handling

| Scenario | Handling |
|---|---|
| Aggregator returns no results | Fall through to direct scraper |
| HTTP 403 / CAPTCHA detected | Mark job `blocked`, retry with Playwright |
| Playwright session fails | Mark job `failed`, log for manual review |
| agent-browser not available | Log warning, skip Google Shopping, continue with other sources |
| Sheet API rate limit | Exponential backoff + batch size reduction to 25 |
| JSON-LD not found | Fall back to CSS selector heuristics |
| EAN not found anywhere | Job marked `done` with `results=[]`, delta shows "—" |
| Network timeout | Retry up to 3x with backoff, then mark `failed` |
| EAN already in Sheet (Mode 1) | Upsert: update name/price/URL if changed |
| EAN already in All Prices tab | Upsert by (EAN, Store Name): update price + timestamp |

---

## Testing Strategy

1. **Unit tests** — each scraper module tested with HTML fixture files (no live requests)
2. **Integration test** — run against a 10-row test Sheet with known EANs; verify correct results
3. **Dry-run test** — verify discovery runs without any writes
4. **Scale test** — 100-product batch: verify rate limiting, parallel workers, batch writes, WAL DB
5. **Resume test** — interrupt mid-run, verify re-run picks up from correct point
6. **Crash recovery test** — manually insert `in_progress` rows into SQLite, verify they reset on startup

---

## Out of Scope (v1)

- Price history tracking (time-series)
- Automated scheduling / cron
- Price alert notifications
- FX currency conversion
- Non-EAN products (handmade, custom)
