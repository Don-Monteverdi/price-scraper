---
name: price-scraper
description: >
  Webshop price intelligence — discovers all products from a client's webshop
  and finds each product on all competitor stores in the country by EAN/GTIN.
  Use when: user says "scrape prices", "price comparison", "compare prices",
  "competitor pricing", "price monitoring", "find cheapest competitors",
  "price intelligence", "check prices against competitors", "price scraper".
---

# Price Scraper Skill

Orchestrates the full price intelligence pipeline: webshop discovery → EAN-based cross-store search → structured JSON output (for Google Sheet import).

## Architecture

```
price-scraper (this skill)
  → price-scraper agent (executor subagent)
    → price_scraper.py (CLI pipeline)
      ├── scrapers/client_webshop.py  (JSON-LD product discovery)
      ├── scrapers/aggregator.py      (arukereso.hu, Google Shopping, idealo)
      └── scrapers/direct.py          (direct competitor fallback)
    → pipeline/job_queue.py           (SQLite state, crash recovery)
    → pipeline/sheet_sync.py          (JSON output)
    → agent-browser                   (Playwright fallback for Google Shopping)
```

## Workflow (5 Steps)

### Step 1 — Gather required params
Collect before dispatching agent:
- `webshop_url` — client webshop URL (optional; skip if sheet already has EANs)
- `sheet_id` — Google Sheet ID for input/output
- `country` — 2-letter code: HU, DE, AT, GB, FR, PL (default: HU)

### Step 2 — Dry run (always first)
Dispatch price-scraper agent with `--dry-run --limit 10`:
- Verify products are discoverable via JSON-LD
- Confirm product count is realistic
- Check no config issues before full run

### Step 3 — Run full pipeline
Dispatch price-scraper agent without `--dry-run`.
- Start: `--workers 5` (conservative, less ban risk)
- Scale: `--workers 10` after first 50 products complete cleanly

### Step 4 — Monitor and handle failures
If agent reports >20% failed/blocked jobs:
- Reduce to `--workers 3`
- Increase `rate_limit_seconds` in `config/sites.json`
- Check if a specific aggregator is blocking

### Step 5 — Present results to user

Report format:
```
Price scrape complete:
- Total products: N
- Coverage: X% (found on at least 1 competitor)
- Cheapest competitor overall: [store name]
- Average delta: +X% (client overpriced by X% on average)
- Output: output/products.json, output/all_prices.json
```

---

## Supported Countries

| Country | Primary Aggregator | Notes |
|---------|-------------------|-------|
| HU | arukereso.hu | Indexes 400+ Hungarian webshops. Best HU coverage. |
| DE | idealo.de | Germany's largest price comparison. |
| AT | idealo.at | Austria |
| FR | idealo.fr | France |
| PL | idealo.pl | Poland |
| GB | Google Shopping only | agent-browser required |
| Other | Google Shopping only | agent-browser required |

## Output Structure

**output/products.json** (one entry per product):
`ean | product_name | client_price | client_currency | client_url | cheapest_price | cheapest_store | delta_percent | stores_count | last_scraped`

**output/all_prices.json** (one entry per product × competitor):
`ean | product_name | store_name | store_url | product_url | price | currency | scraped_at`

**Delta % sign convention:** positive = client overpriced (problem). Negative = client cheaper (opportunity). "—" for cross-currency.

## Constraints

- Google Shopping: requires agent-browser (Playwright); skipped gracefully if unavailable
- EAN coverage: ~80% of physical products have EANs; rest shows no results
- Currency: no FX conversion — cross-currency shows "—"
- Resume: every run is resumable by default (no special flag needed)
- Rate limiting: per-domain delays configurable in `config/sites.json`

## First-Time Setup

```bash
pip install -r requirements.txt
```

Config file: `config/sites.json` — add new countries or competitors here.
