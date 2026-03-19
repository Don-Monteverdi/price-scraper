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

Orchestrates the full price intelligence pipeline: webshop discovery → EAN-based cross-store search → Google Sheet results.

## Architecture

```
price-scraper (this skill)
  → price-scraper agent (executor subagent)
    → tools/price-scraper/price_scraper.py (CLI pipeline)
      ├── scrapers/client_webshop.py  (JSON-LD product discovery)
      ├── scrapers/aggregator.py      (ár.hu, Google Shopping, idealo)
      └── scrapers/direct.py         (Firecrawl fallback)
    → pipeline/job_queue.py          (SQLite state, crash recovery)
    → pipeline/sheet_sync.py         (Google Workspace MCP)
    → agent-browser                  (Playwright fallback)
```

## Workflow (7 Steps)

### Step 1 — Vault check
Grep `~/Documents/SecondBrain/Agent-Brain/Memory/Research/` for previous price scrapes of this domain. If found and fresh (<24h), offer to use cached results.

### Step 2 — Gather required params
Collect before dispatching agent:
- `webshop_url` — client webshop URL (optional; skip if sheet already has EANs)
- `sheet_id` — Google Sheet ID for input/output
- `country` — 2-letter code: HU, DE, AT, GB, FR, PL (default: HU)

### Step 3 — Dry run (always first)
Dispatch price-scraper agent with `--dry-run --limit 10`:
- Verify products are discoverable via JSON-LD
- Confirm product count is realistic
- Check no config issues before full run

### Step 4 — Run full pipeline
Dispatch price-scraper agent without `--dry-run`.
- Start: `--workers 5` (conservative, less ban risk)
- Scale: `--workers 10` after first 50 products complete cleanly

### Step 5 — Monitor and handle failures
If agent reports >20% failed/blocked jobs:
- Reduce to `--workers 3`
- Increase `rate_limit_seconds` in `config/sites.json`
- Check if a specific aggregator is blocking

### Step 6 — Present results to user

Report format:
```
Price scrape complete:
- Total products: N
- Coverage: X% (found on at least 1 competitor)
- Cheapest competitor overall: [store name]
- Average delta: +X% (client overpriced by X% on average)
- Sheet: [Google Sheet link]
```

### Step 7 — Vault
Dispatch vault-scribe with run metadata.

---

## Supported Countries

| Country | Primary Aggregator | Notes |
|---------|-------------------|-------|
| HU | ár.hu | Indexes 400+ Hungarian webshops. Best HU coverage. |
| DE | idealo.de | Germany's largest price comparison. |
| AT | idealo.at | Austria |
| FR | idealo.fr | France |
| PL | idealo.pl | Poland |
| GB | Google Shopping only | agent-browser required |
| Other | Google Shopping only | agent-browser required |

## Google Sheet Output Structure

**Products tab** (one row per product):
`EAN | Product Name | Client Price | Client Currency | Client URL | Cheapest Price | Cheapest Store | Delta % | # Stores Found | Last Scraped`

**All Prices tab** (one row per product × competitor):
`EAN | Product Name | Store Name | Store URL | Product Page URL | Price | Currency | Scraped At`

**Delta % sign convention:** positive = client overpriced (problem). Negative = client cheaper (opportunity). "—" for cross-currency.

## Constraints

- Firecrawl free tier: 500 pages/month — use sparingly, aggregators handle 90%+
- Google Sheets API: batch writes every 50 completions
- Google Shopping: requires agent-browser; skipped gracefully if unavailable
- EAN coverage: ~80% of physical products have EANs; rest shows no results
- Currency: no FX conversion in v1 — cross-currency shows "—"
- Resume: every run is resumable by default (no special flag needed)

## First-Time Setup

```bash
pip install -r tools/price-scraper/requirements.txt
```

Config file: `tools/price-scraper/config/sites.json` — add new countries or competitors here.
