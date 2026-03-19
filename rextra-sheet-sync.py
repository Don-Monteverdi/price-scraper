#!/usr/bin/env python3
"""
Rextra -> Google Sheets sync.
Reads rextra rescrape results JSON and writes:
  - "Arelemzes" tab   : one row per product (summary)
  - "Osszes Ajanlat"  : one row per store offer (all prices flat)

Outputs two JSON files consumed by the scheduler's MCP sheet writes.

Usage:
  python3 tools/price-scraper/rextra-sheet-sync.py
  python3 tools/price-scraper/rextra-sheet-sync.py --input ~/.price-scraper/rextra_rescrape_results.json
  python3 tools/price-scraper/rextra-sheet-sync.py --output /path/to/output/dir
"""

import argparse
import json
import os
import sys

DEFAULT_INPUT = os.path.expanduser("~/.price-scraper/rextra_rescrape_results.json")
DEFAULT_OUTPUT_DIR = os.path.expanduser("~/.price-scraper")


def build_summary_rows(results: list) -> list:
    header = [
        "Termek neve", "Sajat ar (HUF)", "Legolcsobb ar (HUF)",
        "Legolcsobb bolt", "Delta%", "Boltok szama",
        "Megbizhato egyezes", "Termek URL", "Scrape datum",
    ]
    rows = [header]
    for r in results:
        reliable = "igen" if r.get("match_reliable", True) else "nem"
        rows.append([
            r.get("product_name", ""),
            str(r.get("client_price", "")),
            str(r.get("cheapest_price", "")) if r.get("cheapest_price") else "",
            r.get("cheapest_store", ""),
            r.get("delta_percent", "\u2014"),
            str(r.get("stores_count", 0)),
            reliable,
            r.get("client_url", ""),
            r.get("scraped_at", "")[:10],
        ])
    return rows


def build_offers_rows(results: list) -> list:
    header = [
        "Termek neve", "Bolt neve", "Ar (HUF)",
        "Bolt URL", "Termek URL", "Scrape datum",
    ]
    rows = [header]
    for r in results:
        for offer in r.get("all_prices", []):
            rows.append([
                r.get("product_name", ""),
                offer.get("store_name", ""),
                str(int(offer["price"])) if offer.get("price") else "",
                offer.get("store_url", ""),
                offer.get("product_url", ""),
                r.get("scraped_at", "")[:10],
            ])
    return rows


def main():
    parser = argparse.ArgumentParser(description="Rextra results -> Google Sheets JSON sync")
    parser.add_argument("--input", default=DEFAULT_INPUT, metavar="PATH",
                        help=f"Results JSON input (default: {DEFAULT_INPUT})")
    parser.add_argument("--output", default=DEFAULT_OUTPUT_DIR, metavar="DIR",
                        help=f"Output directory for JSON files (default: {DEFAULT_OUTPUT_DIR})")
    args = parser.parse_args()

    try:
        with open(args.input) as f:
            results = json.load(f)
    except FileNotFoundError:
        print(f"ERROR: {args.input} not found -- run rextra-rescrape.py first", file=sys.stderr)
        sys.exit(1)

    summary = build_summary_rows(results)
    offers = build_offers_rows(results)

    os.makedirs(args.output, exist_ok=True)
    summary_path = os.path.join(args.output, "rextra_summary_rows.json")
    offers_path = os.path.join(args.output, "rextra_offers_rows.json")

    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False)

    with open(offers_path, "w", encoding="utf-8") as f:
        json.dump(offers, f, ensure_ascii=False)

    print(f"Summary rows:  {len(summary)-1} products")
    print(f"Offers rows:   {len(offers)-1} store offers")
    print(f"Written: {summary_path}, {offers_path}")


if __name__ == "__main__":
    main()
