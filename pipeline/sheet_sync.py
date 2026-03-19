"""
Google Sheets read/write — local JSON file bridge.

The pipeline writes structured results to local JSON files in an output directory.
The calling agent (price-scraper agent) reads these files and writes to Google Sheets
via MCP tools it already has access to.

For standalone use (no agent), pass --sheet to price_scraper.py and provide a
products JSON file via --input-products.

Sheet structure:
  Tab "Products":    EAN | Product Name | Client Price | Client Currency | Client URL |
                     Cheapest Price | Cheapest Store | Delta % | # Stores Found | Last Scraped
  Tab "All Prices":  EAN | Product Name | Store Name | Store URL | Product Page URL |
                     Price | Currency | Scraped At
"""

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

PRODUCTS_TAB = "Products"
ALL_PRICES_TAB = "All Prices"

PRODUCTS_HEADERS = [
    "EAN", "Product Name", "Client Price", "Client Currency", "Client URL",
    "Cheapest Price", "Cheapest Store", "Delta %", "# Stores Found", "Last Scraped",
]

ALL_PRICES_HEADERS = [
    "EAN", "Product Name", "Store Name", "Store URL", "Product Page URL",
    "Price", "Currency", "Scraped At",
]

# Output directory for JSON files (overridable via env or function param)
DEFAULT_OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "output")


def _get_output_dir(output_dir: str = None) -> str:
    """Get and ensure output directory exists."""
    d = output_dir or os.environ.get("PRICE_SCRAPER_OUTPUT_DIR", DEFAULT_OUTPUT_DIR)
    os.makedirs(d, exist_ok=True)
    return d


def _write_json(filename: str, data, output_dir: str = None):
    """Write data to local JSON file."""
    d = _get_output_dir(output_dir)
    path = os.path.join(d, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info(f"Wrote {path}")
    return path


def _read_json(filename: str, output_dir: str = None):
    """Read data from local JSON file. Returns None if not found."""
    d = _get_output_dir(output_dir)
    path = os.path.join(d, filename)
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


# ── Sheet operations ──────────────────────────────────────────────────────────

def ensure_tabs_exist(sheet_id: str) -> bool:
    """
    Ensure output directory exists with header metadata.
    (In JSON mode, there are no tabs to create — just write headers for reference.)
    """
    d = _get_output_dir()
    _write_json("_headers.json", {
        PRODUCTS_TAB: PRODUCTS_HEADERS,
        ALL_PRICES_TAB: ALL_PRICES_HEADERS,
        "sheet_id": sheet_id,
    })
    return True


def read_products_sheet(sheet_id: str) -> list[dict]:
    """
    Read products from local JSON file (output/products_input.json).
    Falls back to empty list if no file found.

    The calling agent should populate this file from the Google Sheet before
    running the pipeline in sheet-only mode (no --webshop).
    """
    data = _read_json("products_input.json")
    if not data:
        logger.warning("No products_input.json found. Use --webshop to discover products, "
                       "or create output/products_input.json from your Sheet.")
        return []

    products = []
    for row in data:
        ean = str(row.get("ean", "")).strip()
        if not ean:
            continue
        products.append({
            "ean": ean,
            "product_name": row.get("product_name", ""),
            "client_price": _to_float(row.get("client_price")),
            "client_currency": row.get("client_currency", ""),
            "client_url": row.get("client_url", ""),
        })

    logger.info(f"Read {len(products)} products from products_input.json")
    return products


def upsert_products_tab(sheet_id: str, products: list[dict]) -> bool:
    """
    Write product comparison results to output/products.json.
    Each call APPENDS to the existing file (upsert by EAN key).
    """
    if not products:
        return True

    # Load existing data for upsert
    existing = _read_json("products.json") or []
    existing_by_ean = {p["ean"]: i for i, p in enumerate(existing)}

    now = datetime.utcnow().isoformat()

    for p in products:
        ean = p.get("ean", "")
        delta = _calculate_delta(
            p.get("client_price"), p.get("cheapest_price"),
            p.get("client_currency", ""), p.get("cheapest_currency", ""),
            match_reliable=p.get("match_reliable", True),
        )
        row = {
            "ean": ean,
            "product_name": p.get("product_name", ""),
            "client_price": p.get("client_price", ""),
            "client_currency": p.get("client_currency", ""),
            "client_url": p.get("client_url", ""),
            "cheapest_price": p.get("cheapest_price", ""),
            "cheapest_store": p.get("cheapest_store", ""),
            "delta_percent": delta,
            "stores_count": p.get("stores_count", ""),
            "last_scraped": p.get("last_scraped", now),
        }

        if ean in existing_by_ean:
            existing[existing_by_ean[ean]] = row
        else:
            existing.append(row)
            existing_by_ean[ean] = len(existing) - 1

    _write_json("products.json", existing)
    logger.info(f"Upserted {len(products)} rows to products.json")
    return True


def upsert_all_prices_tab(sheet_id: str, price_rows: list[dict]) -> bool:
    """
    Write all individual store prices to output/all_prices.json.
    Upsert by (EAN, Store Name) key.
    """
    if not price_rows:
        return True

    existing = _read_json("all_prices.json") or []
    existing_keys = {}
    for i, row in enumerate(existing):
        key = (row.get("ean", ""), row.get("store_name", ""))
        existing_keys[key] = i

    now = datetime.utcnow().isoformat()

    for p in price_rows:
        ean = p.get("ean", "")
        store_name = p.get("store_name", "")
        key = (ean, store_name)

        row = {
            "ean": ean,
            "product_name": p.get("product_name", ""),
            "store_name": store_name,
            "store_url": p.get("store_url", ""),
            "product_url": p.get("product_url", ""),
            "price": p.get("price", ""),
            "currency": p.get("currency", ""),
            "scraped_at": now,
        }

        if key in existing_keys:
            existing[existing_keys[key]] = row
        else:
            existing.append(row)
            existing_keys[key] = len(existing) - 1

    _write_json("all_prices.json", existing)
    logger.info(f"Upserted {len(price_rows)} rows to all_prices.json")
    return True


def batch_write_results(sheet_id: str, results: list[dict], batch_size: int = 50) -> int:
    """
    Write a batch of EAN comparison results to local JSON output files.
    results: list of {ean, product_name, client_price, client_currency, client_url,
                       cheapest_price, cheapest_store, cheapest_currency, stores_count,
                       all_prices: [{store_name, store_url, product_url, price, currency}]}
    Returns count of results written.
    """
    written = 0
    for i in range(0, len(results), batch_size):
        chunk = results[i:i + batch_size]

        product_updates = []
        all_price_rows = []

        for r in chunk:
            product_updates.append({
                "ean": r["ean"],
                "product_name": r.get("product_name", ""),
                "client_price": r.get("client_price"),
                "client_currency": r.get("client_currency", ""),
                "client_url": r.get("client_url", ""),
                "cheapest_price": r.get("cheapest_price"),
                "cheapest_store": r.get("cheapest_store", ""),
                "cheapest_currency": r.get("cheapest_currency", ""),
                "stores_count": r.get("stores_count", 0),
                "match_reliable": r.get("match_reliable", True),
                "last_scraped": datetime.utcnow().isoformat(),
            })

            for price_entry in r.get("all_prices", []):
                all_price_rows.append({
                    "ean": r["ean"],
                    "product_name": r.get("product_name", ""),
                    **price_entry,
                })

        upsert_products_tab(sheet_id, product_updates)
        upsert_all_prices_tab(sheet_id, all_price_rows)
        written += len(chunk)
        logger.info(f"Output write: {written}/{len(results)} results flushed")

    return written


# ── Helpers ───────────────────────────────────────────────────────────────────

def _to_float(val) -> Optional[float]:
    if val is None or val == "":
        return None
    try:
        return float(str(val).replace(",", "."))
    except (ValueError, TypeError):
        return None


def _calculate_delta(
    client_price: Optional[float],
    cheapest_price: Optional[float],
    client_currency: str,
    cheapest_currency: str,
    match_reliable: bool = True,
) -> str:
    """
    Calculate signed price delta %.
    Positive = client overpriced. Negative = client underpriced.
    Returns warning string when product match is unreliable.
    Returns "—" for cross-currency, unknown-currency, or missing values.
    """
    if not match_reliable:
        return "Összehasonlítás megbízhatatlan (eltérő termék)"
    if client_price is None or cheapest_price is None or cheapest_price == 0:
        return "—"
    # Both currencies must be known and must match
    if not (client_currency and cheapest_currency):
        return "—"
    if client_currency != cheapest_currency:
        return "—"
    delta = (client_price - cheapest_price) / cheapest_price * 100
    sign = "+" if delta > 0 else ""
    return f"{sign}{delta:.1f}%"
