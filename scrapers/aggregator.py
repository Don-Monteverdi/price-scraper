"""
Price aggregator scrapers — search by product name on arukereso.hu, Google Shopping, idealo.
Returns list of {store_name, store_url, product_url, price, currency} per product.

Note: ár.hu (ar.hu) is defunct. The correct Hungarian price comparison site is
arukereso.hu. EAN search on arukereso.hu returns no results; search by product name.
"""

import logging
import re
import shutil
import subprocess
import time
from typing import Optional

import requests
from bs4 import BeautifulSoup

from scrapers.utils import (
    DEFAULT_HEADERS, RateLimiter,
    parse_price as _parse_price,
    name_similarity as _name_similarity,
    extract_base_url as _extract_base_url,
    dedupe_results,
)

logger = logging.getLogger(__name__)


# ── arukereso.hu scraper ──────────────────────────────────────────────────────

ARUKERESO_BASE = "https://www.arukereso.hu"


MATCH_RELIABLE_THRESHOLD = 0.45  # minimum similarity score to trust a product match


def search_arhu(ean: str = "", rate_limiter: RateLimiter = None, product_name: str = "") -> list[dict]:
    """
    Search arukereso.hu for a product by name (EAN search returns no results).
    Returns list of {store_name, store_url, product_url, price, currency, match_reliable}.
    Indexes 400+ Hungarian webshops — single request covers the entire HU market.

    Strategy:
    1. Build candidate queries — full name, then progressively shorter prefixes
       (arukereso returns 0 results for very long/specific queries)
    2. Use first query that returns .product-box results
    3. Find best-matching product box (similarity check vs original product_name)
    4. Follow that product's detail page → extract ALL individual store offers
    5. Mark match_reliable=False when name similarity is below threshold

    Requires product_name — falls back to ean if product_name is empty.
    """
    domain = "arukereso.hu"
    full_query = (product_name or ean).strip()

    # Build candidate queries: full name → shorter prefixes (4+ words)
    words = full_query.split()
    candidates = [full_query]
    for n in range(min(len(words) - 1, 6), 3, -1):  # try 6-word, 5-word, 4-word prefixes
        prefix = " ".join(words[:n])
        if prefix != full_query:
            candidates.append(prefix)

    for query in candidates:
        rate_limiter.wait(domain)
        url = f"{ARUKERESO_BASE}/CategorySearch.php?st={requests.utils.quote(query)}"
        try:
            resp = requests.get(url, headers=DEFAULT_HEADERS, timeout=15)
            resp.raise_for_status()
        except Exception as e:
            logger.warning(f"arukereso.hu request failed for '{query}': {e}")
            continue

        results = _parse_arukereso(resp.text, full_query, rate_limiter)
        if results:
            logger.debug(f"arukereso.hu: found results with query '{query}' (from '{full_query[:40]}')")
            return results

    logger.debug(f"arukereso.hu: no results for any candidate query of '{full_query[:40]}'")
    return []


# _name_similarity imported from scrapers.utils


def _parse_arukereso(html: str, query: str, rate_limiter: RateLimiter) -> list[dict]:
    """
    Parse arukereso.hu search results page.

    Two types of .product-box entries:
      Type A — single-store: shows store domain + price directly
      Type B — aggregate:   shows "N ajánlat" + price → links to a product page with all N stores

    Strategy:
    1. Score every box by name similarity to query
    2. For Type A boxes above MIN_SCORE: extract store + price directly
    3. For Type B boxes above MIN_SCORE: fetch the arukereso product page and extract all stores
    4. Deduplicate to one result per store (cheapest price wins)
    5. match_reliable=False when best similarity score is below MATCH_RELIABLE_THRESHOLD
    """
    soup = BeautifulSoup(html, "lxml")
    product_boxes = soup.select(".product-box")

    if not product_boxes:
        logger.debug(f"arukereso.hu: no .product-box found for '{query}'")
        return []

    # Score and extract all boxes
    scored = []
    for box in product_boxes:
        name_el = box.select_one(".name h2 a")
        box_name = name_el.get_text(strip=True) if name_el else ""
        score = _name_similarity(query, box_name)
        offer = _extract_arukereso_box_offer(box)
        if offer:
            scored.append((score, box_name, offer))

    if not scored:
        return []

    # Determine match quality: best score across all boxes
    best_score = max(s for s, _, _ in scored)
    match_reliable = best_score >= MATCH_RELIABLE_THRESHOLD

    logger.debug(
        f"arukereso.hu: {len(scored)} boxes, best score={best_score:.2f}, "
        f"reliable={match_reliable}, query='{query[:40]}'"
    )

    # Keep only boxes above a minimum similarity bar (filters complete mismatches)
    MIN_SCORE = 0.15
    passing = [(s, name, offer) for s, name, offer in scored if s >= MIN_SCORE]
    if not passing:
        return []  # CODE-D: no boxes above MIN_SCORE — reject all

    # Deduplicate: one entry per store, cheapest price wins
    by_store: dict[str, dict] = {}

    for score, matched_name, offer in passing:
        if offer.get("is_aggregate"):
            # Type B: follow product page to get all individual store offers
            page_offers = _fetch_arukereso_product_page(offer["product_url"], rate_limiter)
            for po in page_offers:
                store = po["store_name"].lower()
                if store not in by_store or po["price"] < by_store[store]["price"]:
                    by_store[store] = {
                        **po,
                        "match_reliable": match_reliable,
                        "matched_product_name": matched_name,
                    }
        else:
            # Type A: single-store offer directly from search results
            store = offer["store_name"].lower()
            if store not in by_store or offer["price"] < by_store[store]["price"]:
                by_store[store] = {
                    **offer,
                    "match_reliable": match_reliable,
                    "matched_product_name": matched_name,
                }

    results = sorted(by_store.values(), key=lambda x: x.get("price") or float("inf"))
    logger.debug(f"arukereso.hu: {len(results)} unique stores for '{query[:40]}'")
    return results


def _fetch_arukereso_product_page(product_url: str, rate_limiter: RateLimiter) -> list[dict]:
    """
    Fetch an arukereso.hu product detail page and extract all individual store offers.

    The product page lists every store that sells this product with their price.
    Typical selectors (arukereso 2025+ layout):
      .offer-list-item, .shop-list tr, [class*='offer-item'], .merchant-row
    Each entry contains: store name, price, link to store's product page.
    """
    domain = "arukereso.hu"
    url = product_url if product_url.startswith("http") else f"{ARUKERESO_BASE}{product_url}"

    rate_limiter.wait(domain)
    try:
        resp = requests.get(url, headers=DEFAULT_HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        logger.debug(f"arukereso product page fetch failed ({url}): {e}")
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    results = []

    # Try multiple selector patterns for the offer list
    offer_rows = (
        soup.select(".offer-list-item")
        or soup.select(".offerList li")
        or soup.select("[class*='offer-item']")
        or soup.select(".shop-list tr")
        or soup.select(".merchant-row")
        or soup.select(".offerItem")
    )

    for row in offer_rows:
        # Store name
        store_el = (
            row.select_one(".merchant-name")
            or row.select_one(".shop-name")
            or row.select_one("[class*='shop-name']")
            or row.select_one("[class*='merchant']")
            or row.select_one("a[class*='shop']")
        )
        store_name = store_el.get_text(strip=True) if store_el else ""
        if not store_name:
            continue

        # Price
        price_el = (
            row.select_one("[class*='price'] .price")
            or row.select_one("a.price")
            or row.select_one("[class*='price']")
            or row.select_one("[itemprop='price']")
        )
        if not price_el:
            continue
        price_text = price_el.get("content") or price_el.get_text(strip=True)
        price = _parse_price(price_text)
        if price is None:
            continue

        # Store product URL (SEC-F: validate scheme)
        link_el = row.select_one("a[href]")
        store_product_url = link_el["href"] if link_el else url
        if store_product_url.startswith("/"):
            store_product_url = f"{ARUKERESO_BASE}{store_product_url}"
        elif not store_product_url.startswith(("http://", "https://")):
            store_product_url = url  # fallback to parent URL

        results.append({
            "store_name": store_name,
            "store_url": f"https://www.{store_name}" if "." in store_name else ARUKERESO_BASE,
            "product_url": store_product_url,
            "price": price,
            "currency": "HUF",
        })

    logger.debug(f"arukereso product page: {len(results)} store offers from {url}")
    return results


def _extract_arukereso_box_offer(box) -> Optional[dict]:
    """
    Extract store name + price from a single .product-box search result card.

    Structure (from live inspection 2026-03-12):
      .col-lg-3.top-right  — contains:
        a.price             — price text, e.g. "63 488 Ft"
        .offer-num          — store domain name OR "N ajánlat" aggregate count
      .ak-info.hidden-xs   — product detail page URL on arukereso

    Returns either:
      - A single-store offer dict (store_name, price, etc.)
      - An aggregate sentinel dict (is_aggregate=True, product_url, price) for "N ajánlat" boxes
      - None if the box has no usable price
    """
    top_right = box.select_one(".top-right")
    price_el = (top_right.select_one("a.price") if top_right else None) or box.select_one("a.price")
    if not price_el:
        return None

    price = _parse_price(price_el.get_text(strip=True))
    if price is None:
        return None

    offer_el = (top_right.select_one(".offer-num") if top_right else None) or box.select_one(".offer-num")
    store_raw = offer_el.get_text(strip=True) if offer_el else ""

    detail_link = box.select_one("a.ak-info")
    product_url = detail_link["href"] if detail_link and detail_link.get("href") else ""

    # "N ajánlat" boxes aggregate multiple stores behind an arukereso product page
    if re.match(r"^\d+\s+ajánlat", store_raw):
        if not product_url:
            return None
        return {"is_aggregate": True, "product_url": product_url, "price": price}

    if not store_raw:
        return None

    return {
        "store_name": store_raw,
        "store_url": f"https://www.{store_raw}" if "." in store_raw else ARUKERESO_BASE,
        "product_url": product_url,
        "price": price,
        "currency": "HUF",
    }


# ── Google Shopping scraper (agent-browser / Playwright) ─────────────────────

AGENT_BROWSER_BIN = "agent-browser"


def _agent_browser_available() -> bool:
    """Check if agent-browser CLI is on PATH."""
    return shutil.which(AGENT_BROWSER_BIN) is not None


def search_google_shopping(
    ean: str, country_code: str, rate_limiter: RateLimiter
) -> list[dict]:
    """
    Search Google Shopping for a product by EAN.
    Uses agent-browser (Playwright) because Google aggressively blocks headless HTTP.
    Returns [] with a warning if agent-browser is unavailable.
    """
    if not _agent_browser_available():
        logger.warning(
            "Google Shopping skipped — agent-browser not available. "
            "Install it or use another aggregator."
        )
        return []

    domain = "google.com"
    rate_limiter.wait(domain)

    from scrapers.utils import quote
    query = ean
    url = f"https://www.google.com/search?q={quote(query, safe='')}&tbm=shop&gl={country_code.lower()}"

    try:
        result = subprocess.run(
            [AGENT_BROWSER_BIN, "open", url],
            capture_output=True, text=True, timeout=30,
        )
        # PERF-A: Use networkidle wait instead of fixed sleep
        subprocess.run(
            [AGENT_BROWSER_BIN, "wait", "--load", "networkidle"],
            capture_output=True, text=True, timeout=15,
        )

        snapshot_result = subprocess.run(
            [AGENT_BROWSER_BIN, "snapshot", "-c"],
            capture_output=True, text=True, timeout=15,
        )
        snapshot_text = snapshot_result.stdout
    except subprocess.TimeoutExpired:
        logger.warning(f"Google Shopping timeout for EAN {ean}")
        return []
    except Exception as e:
        logger.warning(f"Google Shopping agent-browser error for EAN {ean}: {e}")
        return []

    return _parse_google_shopping_snapshot(snapshot_text, ean)


def _parse_google_shopping_snapshot(snapshot: str, ean: str) -> list[dict]:
    """
    Parse accessibility tree snapshot from agent-browser.
    Google Shopping results typically appear as list items with price + store name.
    """
    results = []

    # Pattern: find price + store name pairs in the accessibility tree
    # Format varies; look for common Shopping result patterns
    price_pattern = re.compile(r'(\d[\d\s.,]+)\s*(Ft|HUF|EUR|USD|GBP|PLN)', re.IGNORECASE)
    store_pattern = re.compile(r'(?:from|at|by|–|—)\s+([A-Za-z0-9\s.hu-]{3,40})', re.IGNORECASE)

    # Split by Shopping result blocks (heuristic: lines with prices)
    lines = snapshot.splitlines()
    current_block: dict = {}

    for line in lines:
        price_match = price_pattern.search(line)
        if price_match:
            price_raw = price_match.group(1).replace(" ", "").replace(".", "").replace(",", ".")
            try:
                current_block["price"] = float(price_raw)
                current_block["currency"] = price_match.group(2).upper()
                if current_block["currency"] == "FT":
                    current_block["currency"] = "HUF"
            except ValueError:
                pass

        store_match = store_pattern.search(line)
        if store_match and "price" in current_block:
            current_block["store_name"] = store_match.group(1).strip()

        # URL detection
        if "http" in line and "price" in current_block:
            url_match = re.search(r'https?://[^\s"\']+', line)
            if url_match:
                current_block["product_url"] = url_match.group(0)
                current_block["store_url"] = _extract_base_url(current_block["product_url"])

        # Flush block when we have enough data
        if "price" in current_block and "store_name" in current_block:
            results.append({
                "store_name": current_block.get("store_name", ""),
                "store_url": current_block.get("store_url", ""),
                "product_url": current_block.get("product_url", ""),
                "price": current_block["price"],
                "currency": current_block.get("currency", ""),
            })
            current_block = {}

    logger.debug(f"Google Shopping: {len(results)} results for EAN {ean}")
    return results


# ── idealo scraper ────────────────────────────────────────────────────────────

def search_idealo(ean: str, tld: str, rate_limiter: RateLimiter) -> list[dict]:
    """
    Search idealo.{tld} for a product by EAN.
    tld: 'de', 'at', 'fr', 'it', 'es', 'pl', etc.
    """
    domain = f"idealo.{tld}"
    rate_limiter.wait(domain)
    url = (
        f"https://www.idealo.{tld}/preisvergleich/"
        f"MainSearchProductCategory.html?q={ean}"
    )

    try:
        resp = requests.get(url, headers=DEFAULT_HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        logger.warning(f"idealo.{tld} request failed for EAN {ean}: {e}")
        return []

    return _parse_idealo(resp.text, tld, ean)


def _parse_idealo(html: str, tld: str, ean: str) -> list[dict]:
    """Parse idealo search results."""
    soup = BeautifulSoup(html, "lxml")
    results = []

    # idealo product offer cards
    offer_blocks = soup.select(
        ".productOffers-listItemOffer, .offer-list-item, [class*='offerItem']"
    )

    for block in offer_blocks:
        try:
            result = _extract_idealo_offer(block, tld)
            if result:
                results.append(result)
        except Exception as e:
            logger.debug(f"idealo parse error: {e}")

    logger.debug(f"idealo.{tld}: {len(results)} results for EAN {ean}")
    return results


def _extract_idealo_offer(block, tld: str) -> Optional[dict]:
    """Extract a single offer from an idealo result block."""
    # Store name
    store_el = block.select_one(".shop-name, [class*='shopName'], .merchantName")
    if not store_el:
        return None
    store_name = store_el.get_text(strip=True)

    # Product URL (idealo affiliate link)
    link_el = block.select_one("a[href]")
    product_url = ""
    if link_el:
        href = link_el["href"]
        product_url = f"https://www.idealo.{tld}{href}" if href.startswith("/") else href

    # Price
    price_el = block.select_one(".price, [class*='price'], [itemprop='price']")
    if not price_el:
        return None
    price_text = price_el.get("content") or price_el.get_text(strip=True)
    price = _parse_price(price_text)
    if price is None:
        return None

    currency = {
        "de": "EUR", "at": "EUR", "fr": "EUR", "it": "EUR", "es": "EUR",
        "pl": "PLN", "gb": "GBP", "ch": "CHF",
    }.get(tld, "")

    return {
        "store_name": store_name,
        "store_url": f"https://www.idealo.{tld}",
        "product_url": product_url,
        "price": price,
        "currency": currency,
    }


# ── Aggregator dispatcher ─────────────────────────────────────────────────────

def search_aggregators(
    ean: str,
    country_config: dict,
    rate_limiter: RateLimiter,
    product_name: str = "",
) -> list[dict]:
    """
    Search all configured aggregators for this country.
    Returns deduplicated list of price results sorted by price ascending.
    product_name is required for arukereso.hu (HU market) — EAN search is not supported.
    """
    all_results: list[dict] = []
    aggregators = country_config.get("aggregators", [])
    country_code = country_config.get("country_code") or country_config.get("currency", "HU")[:2]

    for agg in aggregators:
        try:
            if agg in ("arhu", "arukereso"):
                results = search_arhu(ean, rate_limiter, product_name=product_name)  # rate_limiter used internally for product page fetch too
            elif agg == "google_shopping":
                results = search_google_shopping(ean, country_code, rate_limiter)
            elif agg == "idealo":
                tld = country_config.get("idealo_tld", "de")
                results = search_idealo(ean, tld, rate_limiter)
            else:
                logger.warning(f"Unknown aggregator: {agg}")
                results = []
            all_results.extend(results)
        except Exception as e:
            logger.error(f"Aggregator {agg} failed for EAN {ean}: {e}")

    return dedupe_results(all_results)


# ── Helpers ───────────────────────────────────────────────────────────────────

# _parse_price and _extract_base_url imported from scrapers.utils
