# Price Intelligence Pipeline

A Python CLI that discovers all products from a client's webshop and compares their prices against every competitor in the country — automatically, resumably, and at scale.

```
python3 price_scraper.py --webshop https://www.example.com --sheet SHEET_ID --country HU
```

## What It Does

1. **Discovers products** from a client's webshop via JSON-LD `schema.org/Product` markup (sitemap.xml first, BFS crawl fallback)
2. **Extracts EAN/GTIN barcodes** using a priority chain: `gtin13 > gtin14 > gtin8 > gtin > sku`
3. **Searches price aggregators** (arukereso.hu, Google Shopping, idealo) for each product by EAN
4. **Falls back to direct competitor scraping** when aggregators miss a product
5. **Writes full price comparison** to local JSON files (structured for Google Sheet import)
6. **Resumes automatically** — every run skips already-done jobs via a SQLite job queue

## Architecture

```
agent/
├── agent.md                    Claude Code agent definition
├── skill.md                    Claude Code skill (7-step workflow)
└── output-contract.json        Output validation schema

price_scraper.py                CLI entrypoint + orchestration
├── scrapers/
│   ├── utils.py                Shared: price parser, rate limiter, dedup, similarity
│   ├── client_webshop.py       Product discovery (sitemap + BFS + JSON-LD + CSS heuristics)
│   ├── aggregator.py           Price aggregators (arukereso.hu, Google Shopping, idealo)
│   └── direct.py               Direct competitor site search (EAN + name-based)
├── pipeline/
│   ├── job_queue.py            SQLite WAL job queue (crash-safe, atomic claims)
│   ├── worker.py               ThreadPoolExecutor parallel workers
│   └── sheet_sync.py           Structured JSON output (for Google Sheet import)
├── config/
│   └── sites.json              Country → aggregators, competitors, rate limits
├── tests/                      60 unit tests (pytest)
└── output/                     JSON results (auto-created)
```

### Data Flow

```
[--webshop URL]
    │
    ├── sitemap.xml → product URLs
    │   (fallback: BFS crawl from homepage)
    │
    ├── Per product URL:
    │   ├── JSON-LD extraction (@type: Product)
    │   └── CSS heuristic fallback
    │
    └── Products with EANs
            │
            ├── SQLite job queue (WAL mode, crash-safe)
            │
            └── ThreadPoolExecutor (N workers, default 10)
                    │
                    ├── Aggregator search (per country config)
                    │   ├── arukereso.hu (HU) — by product name
                    │   ├── Google Shopping — by EAN (via agent-browser)
                    │   └── idealo (DE/AT/FR/PL) — by EAN
                    │
                    ├── Direct competitor search (fallback)
                    │   └── Search known competitor domains by EAN or name
                    │
                    └── Results → output/products.json + output/all_prices.json
```

## Quick Start

### Installation

```bash
git clone https://github.com/Don-Monteverdi/price-scraper.git
cd price-scraper
pip install -r requirements.txt
```

### Usage

```bash
# Discover products from a webshop + compare prices (HU market)
python3 price_scraper.py \
  --webshop https://www.example.com \
  --sheet MY_SHEET_ID \
  --country HU \
  --workers 5

# Dry run — test discovery without any writes
python3 price_scraper.py \
  --webshop https://www.example.com \
  --sheet MY_SHEET_ID \
  --dry-run --limit 10

# Resume after interruption (automatic — skips done jobs)
python3 price_scraper.py --sheet MY_SHEET_ID --country HU

# Force re-scrape everything (ignore freshness window)
python3 price_scraper.py --sheet MY_SHEET_ID --country HU --force-refresh

# Use a specific country (DE uses idealo + Google Shopping)
python3 price_scraper.py --webshop https://www.example.de --sheet ID --country DE
```

### CLI Options

| Flag | Default | Description |
|------|---------|-------------|
| `--webshop URL` | — | Client webshop URL to discover products from |
| `--sheet ID` | required | Google Sheet ID for input/output |
| `--country CODE` | `HU` | Country code (HU, DE, AT, GB, FR, PL) |
| `--workers N` | `10` | Parallel workers |
| `--max-age-hours N` | `24` | Re-scrape EANs older than N hours |
| `--force-refresh` | — | Reset all jobs to pending |
| `--dry-run` | — | No writes — show what would happen |
| `--limit N` | — | Process only first N products |
| `--db-path PATH` | `.price-scraper.db` | SQLite DB path |
| `--verbose` | — | Debug logging |

## Output

Results are written to the `output/` directory as JSON files:

### `output/products.json` — Product comparison summary

```json
[
  {
    "ean": "5707055196264",
    "product_name": "Littmann Classic III",
    "client_price": 45990,
    "client_currency": "HUF",
    "client_url": "https://www.example.com/product/123",
    "cheapest_price": 39990,
    "cheapest_store": "competitor.hu",
    "delta_percent": "+15.0%",
    "stores_count": 7,
    "last_scraped": "2026-03-19T14:30:00"
  }
]
```

### `output/all_prices.json` — All individual store offers

```json
[
  {
    "ean": "5707055196264",
    "product_name": "Littmann Classic III",
    "store_name": "competitor.hu",
    "store_url": "https://www.competitor.hu",
    "product_url": "https://www.competitor.hu/product/abc",
    "price": 39990,
    "currency": "HUF",
    "scraped_at": "2026-03-19T14:30:00"
  }
]
```

### Delta % Convention

- **Positive** = client is overpriced vs cheapest competitor (problem)
- **Negative** = client is cheaper (opportunity to raise or use as marketing)
- **"—"** = cross-currency comparison or missing data
- Formula: `(client_price - cheapest_price) / cheapest_price * 100`

## Supported Countries

| Country | Aggregators | Currency |
|---------|-------------|----------|
| **HU** | arukereso.hu + Google Shopping | HUF |
| **DE** | idealo.de + Google Shopping | EUR |
| **AT** | idealo.at + Google Shopping | EUR |
| **GB** | Google Shopping | GBP |
| **FR** | idealo.fr + Google Shopping | EUR |
| **PL** | idealo.pl + Google Shopping | PLN |

Add new countries by editing `config/sites.json`.

## How Product Discovery Works

### Step 1: Sitemap (fastest path)
Fetches `{webshop}/sitemap.xml`, handles sitemap indexes, filters URLs matching product patterns (`/product/`, `/termek/`, `/p/`, etc.).

### Step 2: BFS Crawl (fallback)
If no sitemap: crawls from the homepage following internal links, collecting URLs matching product patterns. Max 5,000 pages.

### Step 3: Product Extraction
For each product URL:
1. **JSON-LD** (`<script type="application/ld+json">` with `@type: Product`) — works on ~90% of modern e-commerce platforms
2. **CSS heuristics** (fallback) — common selectors for price/EAN fields

### EAN Priority Chain
`gtin13` > `gtin14` > `gtin8` > `gtin` > `sku` (logged as "SKU as EAN proxy")

## How Price Comparison Works

### Aggregator Cascade
1. **Primary:** Search configured aggregators (arukereso.hu, idealo, Google Shopping)
2. **Fallback:** If < 2 results, also search known competitor domains directly
3. **Deduplication:** By `(store_name, price)` — cheapest per store wins

### arukereso.hu (HU market)
- Indexes 400+ Hungarian webshops in a single request
- Searches by **product name** (not EAN — arukereso doesn't support EAN search)
- Follows "N ajánlat" aggregate pages to extract ALL individual store offers
- Name similarity matching prevents false positives (threshold: 0.45)

### Google Shopping
- Requires [agent-browser](https://github.com/nicholasoxford/agent-browser) (Playwright-based) — Google blocks headless HTTP
- Skips gracefully if agent-browser is unavailable

### idealo (DE, AT, FR, PL)
- Searches by EAN directly via HTTP
- Supports: `.de`, `.at`, `.fr`, `.it`, `.es`, `.pl`, `.co.uk`, `.ch`

## Reliability

### SQLite Job Queue
- **WAL mode** — concurrent reads, single writer thread
- **Crash recovery** — stale `in_progress` jobs reset to `pending` on startup (> 5 min threshold)
- **Atomic claims** — `BEGIN IMMEDIATE` transaction prevents double-processing
- **Resumable** — every run picks up where the last one left off
- **Freshness window** — `--max-age-hours` re-scrapes stale results (default: 24h)

### Job Status Flow
```
pending → in_progress → done
                     ↘ failed (retried up to 3x)
                     ↘ blocked (CAPTCHA/403 detected)
```

### Rate Limiting
- Thread-safe per-domain rate limiter with `threading.Lock`
- Configurable per country in `config/sites.json`
- Separate limits for webshop crawl vs aggregator requests

## Security

- **SSRF protection** — `--webshop` URL validated: blocks private IPs, loopback, non-HTTP schemes
- **Input validation** — EAN format validated, competitor domains checked for path injection
- **JSON-LD bounds** — scripts > 1MB skipped, items list capped at 200
- **URL scheme validation** — only `http://` and `https://` hrefs propagated
- **No hardcoded secrets** — Sheet IDs via environment variables
- **Thread-safe** — `RateLimiter` uses `threading.Lock`, `WriteQueue` uses `queue.Queue`

## Testing

```bash
pip install -r requirements-dev.txt
python3 -m pytest tests/ -v
```

**60 tests** covering:
- Price parsing (EU, US, HUF formats, edge cases)
- Rate limiter thread safety
- Job queue atomicity and crash recovery
- Delta calculation (cross-currency, empty values)
- SSRF and domain validation
- Utility functions (dedup, similarity, URL extraction)

## Configuration

### `config/sites.json`

```json
{
  "HU": {
    "aggregators": ["arukereso", "google_shopping"],
    "competitors": ["competitor1.hu", "competitor2.hu"],
    "currency": "HUF",
    "country_code": "HU",
    "language": "hu",
    "crawl_rate_limit": 0.5,
    "aggregator_rate_limit": 2.5
  }
}
```

| Field | Description |
|-------|-------------|
| `aggregators` | List of aggregator keys to search |
| `competitors` | Direct competitor domains for fallback scraping |
| `currency` | ISO 4217 currency code |
| `country_code` | ISO 3166-1 alpha-2 country code |
| `crawl_rate_limit` | Seconds between webshop crawl requests |
| `aggregator_rate_limit` | Seconds between aggregator requests |
| `idealo_tld` | idealo TLD for this country (de, at, fr, etc.) |

### Adding a New Country

1. Add entry to `config/sites.json`
2. If using a new aggregator, implement a `search_*()` function in `scrapers/aggregator.py`
3. Add the aggregator key to the dispatcher in `search_aggregators()`

## Rextra Scripts

Two standalone scripts for a specific client use case (Rextra medical supplies):

```bash
# Re-scrape Littmann products by name (no EAN needed)
REXTRA_SHEET_ID=your_id python3 rextra-rescrape.py --input products.json --output results.json

# Sync results to Google Sheet format
python3 rextra-sheet-sync.py --input results.json
```

These demonstrate name-based search when products lack standard EANs.

## Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| `requests` | >= 2.32.3 | HTTP client |
| `beautifulsoup4` | >= 4.12.0 | HTML parsing |
| `lxml` | >= 5.1.0 | Fast XML/HTML parser |
| `tqdm` | >= 4.66.0 | Progress bars |

**Optional:** [agent-browser](https://github.com/nicholasoxford/agent-browser) for Google Shopping (Playwright-based headless browser).

## Claude Code Agent Integration

This repo includes a full Claude Code agent + skill for hands-free operation. The agent orchestrates the CLI pipeline, handles Google Sheet I/O via MCP tools, and validates output against a contract.

### Files

| File | Purpose |
|------|---------|
| `agent/agent.md` | Agent definition — triggers on "scrape prices", "price comparison", etc. |
| `agent/skill.md` | 7-step skill workflow: vault check → params → dry run → full run → monitor → report → vault |
| `agent/output-contract.json` | Output schema validated by SubagentStop hook |
| `docs/design-spec.md` | Original approved design spec |

### How the Agent Works

```
User: "Compare prices for example.com against all HU competitors"
  │
  ├── Skill activates (price-scraper skill)
  │     ├── Step 1: Gather params (webshop URL, sheet ID, country)
  │     └── Step 2: Dispatch price-scraper agent
  │
  └── Agent executes (price-scraper agent)
        ├── Dry run first (--dry-run --limit 10)
        ├── Full pipeline (--workers 5, scale to 10)
        ├── Read output/products.json → write to Google Sheet via MCP
        └── Report summary to user
```

### Using with Claude Code

1. Copy `agent/agent.md` to `.claude/agents/price-scraper.md`
2. Copy `agent/skill.md` to `.claude/skills/price-scraper/SKILL.md`
3. Copy `agent/output-contract.json` to `data/contracts/price-scraper.json`
4. The agent triggers automatically when users mention price comparison

### Output Contract

The agent's output is validated against `output-contract.json`. Required fields:

```json
{
  "total_products": 500,
  "products_with_results": 423,
  "coverage_percent": 84.6,
  "sheet_id": "1ABC...",
  "country": "HU",
  "scraped_at": "2026-03-19T14:30:00"
}
```

## License

MIT

## Credits

Built by [4YES](https://4yes.hu) as part of the price intelligence consultation toolkit.
