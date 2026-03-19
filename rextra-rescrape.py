#!/usr/bin/env python3
"""
Rextra Littmann re-scrape — uses updated aggregator with:
- Product name similarity matching (picks best arukereso result, not all)
- "N ajánlat" aggregate box following (gets ALL stores from arukereso product pages)
- Direct name-based competitor search (catches stores not on arukereso)
- match_reliable flag (skips delta% when product identity is uncertain)
- LIMITÁLT fallback (falls back to family query for limited-edition SKUs)

Run: python3 /tmp/rextra_rescrape.py
"""

import argparse
import json
import os
import sys
import time
import logging
from datetime import datetime
from pathlib import Path

# Add the price-scraper directory to path (portable, not hardcoded)
sys.path.insert(0, str(Path(__file__).resolve().parent))

from scrapers.utils import RateLimiter, _name_similarity, dedupe_results
from scrapers.aggregator import search_arhu
from scrapers.direct import search_all_competitors_by_name

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

SHEET_ID = os.environ.get("REXTRA_SHEET_ID")
if not SHEET_ID:
    raise EnvironmentError(
        "REXTRA_SHEET_ID environment variable is required. "
        "Set it to your Google Sheet ID before running."
    )
PRICE_FLOOR = 0        # No floor for product list — include all accessories
OFFER_MIN_PRICE = 500  # Filter junk offers below this (e.g. 8.99 HUF back-brace false positives)

# Direct competitor domains to search by name (for stores not on arukereso)
COMPETITOR_DOMAINS = [
    "winterthurmedical.com",
    "plazapatika.hu",
    "orvosieszkoz.hu",
    "medistore.hu",
    "orvos-medtech.hu",
    "doktorshop.hu",
    "orvosikeszulek.hu",
    "medicalcenter.hu",
    "meddoc.hu",
]

# Family name extraction — arukereso returns 0 results for long product names.
# Extract the shortest useful search term (brand + model family, strip color/SKU).
FAMILY_PATTERNS = [
    ("Littmann Classic II Infant", "Littmann Classic II Infant"),
    ("Littmann Classic II Pediatric", "Littmann Classic II Pediatric"),
    ("Littmann Classic III", "Littmann Classic III"),
    ("Littmann Cardiology IV", "Littmann Cardiology IV"),
    ("Littmann Cardio", "Littmann Cardiology IV"),  # abbreviated variant
    ("Littmann Master Cardiology", "Littmann Master Cardiology"),
    ("Littmann Core", "Littmann Core digitális"),
]

def extract_family_query(product_name: str) -> str:
    """Return shortest useful arukereso search query for this product name."""
    for pattern, query in FAMILY_PATTERNS:
        if pattern.lower() in product_name.lower():
            return query
    return product_name  # fallback: let aggregator truncate


# _dedupe_results replaced by dedupe_results from scrapers.utils


DEFAULT_INPUT_PATH = os.path.expanduser("~/.price-scraper/rextra_products.json")
DEFAULT_OUTPUT_PATH = os.path.expanduser("~/.price-scraper/rextra_rescrape_results.json")


def load_products(input_path: str = DEFAULT_INPUT_PATH):
    try:
        with open(input_path) as f:
            all_products = json.load(f)
    except FileNotFoundError:
        logger.error(f"Input file not found: {input_path}")
        sys.exit(1)
    products = [
        p for p in all_products
        if "Littmann" in p.get("name", "") and p.get("price", 0) >= PRICE_FLOOR
    ]
    logger.info(f"Loaded {len(products)} Littmann products ≥{PRICE_FLOOR:,} HUF")
    return products


def scrape_product(product: dict, rate_limiter: RateLimiter) -> dict:
    name = product["name"]
    client_price = product["price"]
    # ARCH-H: Use raw name first (not family query) — family query is fallback
    search_query = name
    limited_edition_fallback = False

    # 1. arukereso.hu — covers all listed stores, now follows "N ajánlat" aggregate pages
    results = search_arhu("", rate_limiter, product_name=search_query)
    results = [r for r in results if (r.get("price") or 0) >= OFFER_MIN_PRICE]

    # 2. Direct name-based search on known HU competitor stores (catches stores not on arukereso)
    direct_results = search_all_competitors_by_name(
        search_query, COMPETITOR_DOMAINS, rate_limiter, price_floor=OFFER_MIN_PRICE
    )
    results = dedupe_results(results + direct_results)

    # 3. LIMITÁLT fallback — if still empty, try family name (strips color/variant/SKU)
    if not results:
        family_query = extract_family_query(name)
        if family_query != search_query:
            logger.info(f"  → No results, trying LIMITÁLT family fallback: '{family_query}'")
            fallback = search_arhu("", rate_limiter, product_name=family_query)
            fallback = [r for r in fallback if (r.get("price") or 0) >= OFFER_MIN_PRICE]
            if fallback:
                results = dedupe_results(fallback)
                limited_edition_fallback = True

    cheapest = results[0] if results else {}
    match_reliable = all(r.get("match_reliable", True) for r in results) if results else True
    matched_name = results[0].get("matched_product_name", "") if results else ""

    # Delta%
    if limited_edition_fallback:
        delta = "Közelítő ár (limitált kiadás, más szín/variáns alapján)"
    elif not match_reliable:
        delta = "Összehasonlítás megbízhatatlan (eltérő termék)"
    elif cheapest.get("price") and cheapest["price"] > 0:
        d = (client_price - cheapest["price"]) / cheapest["price"] * 100
        sign = "+" if d > 0 else ""
        delta = f"{sign}{d:.1f}%"
    else:
        delta = "—"

    return {
        "product_name": name,
        "client_price": client_price,
        "client_currency": "HUF",
        "client_url": product.get("url", ""),
        "cheapest_price": cheapest.get("price"),
        "cheapest_store": cheapest.get("store_name", ""),
        "stores_count": len(results),
        "match_reliable": match_reliable,
        "matched_product_name": matched_name,
        "limited_edition_fallback": limited_edition_fallback,
        "delta_percent": delta,
        "all_prices": results,
        "scraped_at": datetime.utcnow().isoformat(),
    }


def parse_args():
    parser = argparse.ArgumentParser(description="Rextra Littmann price re-scrape")
    parser.add_argument("--input", default=DEFAULT_INPUT_PATH, metavar="PATH",
                        help=f"Products JSON input file (default: {DEFAULT_INPUT_PATH})")
    parser.add_argument("--output", default=DEFAULT_OUTPUT_PATH, metavar="PATH",
                        help=f"Results JSON output file (default: {DEFAULT_OUTPUT_PATH})")
    return parser.parse_args()


def main():
    args = parse_args()
    products = load_products(args.input)
    rate_limiter = RateLimiter(delay_seconds=2.5)
    results = []

    logger.info(f"Scraping {len(products)} products — sequential (arukereso rate limit)")

    for i, product in enumerate(products, 1):
        logger.info(f"[{i}/{len(products)}] {product['name'][:60]}")
        try:
            result = scrape_product(product, rate_limiter)
            results.append(result)

            stores = result["stores_count"]
            cheapest = result.get("cheapest_price")
            delta = result["delta_percent"]
            reliable = "✓" if result["match_reliable"] else "⚠ eltérő termék"
            logger.info(
                f"  → {stores} stores | cheapest: {cheapest} HUF @ {result['cheapest_store'][:30]} "
                f"| delta: {delta} | {reliable}"
            )
        except Exception as e:
            logger.error(f"  Failed: {e}")
            results.append({
                "product_name": product["name"],
                "client_price": product["price"],
                "client_currency": "HUF",
                "client_url": product.get("url", ""),
                "cheapest_price": None,
                "cheapest_store": "",
                "stores_count": 0,
                "match_reliable": True,
                "matched_product_name": "",
                "delta_percent": "—",
                "all_prices": [],
                "scraped_at": datetime.utcnow().isoformat(),
                "error": str(e),
            })

    # Save raw results
    output_path = args.output
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    logger.info(f"Saved {len(results)} results → {output_path}")

    # Summary stats
    matched = [r for r in results if r["stores_count"] > 0]
    reliable = [r for r in matched if r["match_reliable"]]
    unreliable = [r for r in matched if not r["match_reliable"]]
    total_stores = sum(r["stores_count"] for r in matched)

    print("\n" + "═" * 60)
    print("  Rextra Re-scrape Complete")
    print("═" * 60)
    print(f"  Products scraped:     {len(results)}")
    print(f"  With results:         {len(matched)} ({len(matched)/len(results)*100:.0f}%)")
    print(f"  Reliable matches:     {len(reliable)}")
    print(f"  Unreliable matches:   {len(unreliable)} (⚠ eltérő termék)")
    print(f"  Total store offers:   {total_stores}")
    print(f"  Avg stores/product:   {total_stores/len(matched):.1f}" if matched else "")
    print(f"  Output:               {output_path}")
    print("═" * 60)

    # Top overpriced products (reliable matches only)
    if reliable:
        print("\nTop overpriced vs cheapest competitor:")
        sortable = []
        for r in reliable:
            try:
                d = (r["client_price"] - r["cheapest_price"]) / r["cheapest_price"] * 100
                sortable.append((d, r))
            except (TypeError, ZeroDivisionError):
                pass
        sortable.sort(key=lambda x: x[0], reverse=True)
        for delta, r in sortable[:10]:
            print(f"  {delta:+.1f}%  {r['client_price']:>8,.0f} vs {r['cheapest_price']:>8,.0f} HUF"
                  f" @ {r['cheapest_store'][:25]:<25}  {r['product_name'][:50]}")

    return results


if __name__ == "__main__":
    main()
