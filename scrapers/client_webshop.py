"""
Client webshop product discovery.
Crawls a webshop URL, extracts all products via JSON-LD schema.org Product markup.
Fallback: CSS selector heuristics for sites without JSON-LD.
"""

import ipaddress
import json
import logging
import re
import socket
import time
from collections import deque
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# EAN field priority: first match in this order wins
EAN_FIELDS = ["gtin13", "gtin14", "gtin8", "gtin", "sku"]

# URL path patterns that typically indicate product pages
PRODUCT_URL_PATTERNS = [
    r"/termek/", r"/product/", r"/p/", r"/products/",
    r"/item/", r"/catalog/product/", r"/shop/product/",
    r"/[a-z0-9-]+-p-\d+", r"/[a-z0-9-]+-\d+\.html",
]

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "hu-HU,hu;q=0.9,en-US;q=0.8,en;q=0.7",
}

REQUEST_DELAY = 0.5  # seconds between requests during crawl

# Module-level requests.Session for connection pooling (PERF-E)
_session = requests.Session()
_session.headers.update(DEFAULT_HEADERS)


def _assert_safe_url(url: str) -> None:
    """Block SSRF: reject private/reserved IPs and non-HTTPS schemes."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Blocked URL with scheme '{parsed.scheme}': {url}")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError(f"No hostname in URL: {url}")
    try:
        resolved_ip = socket.gethostbyname(hostname)
        ip = ipaddress.ip_address(resolved_ip)
        if ip.is_private or ip.is_reserved or ip.is_loopback or ip.is_link_local:
            raise ValueError(
                f"Blocked private/reserved IP {resolved_ip} for hostname '{hostname}'"
            )
    except socket.gaierror:
        raise ValueError(f"Cannot resolve hostname: {hostname}")


def discover_products(
    webshop_url: str,
    max_pages: int = 5000,
    max_products: int = None,
    rate_limit_seconds: float = REQUEST_DELAY,
) -> list[dict]:
    """
    Discover all products from a webshop.
    Returns list of {ean, product_name, client_price, client_currency, client_url}.
    Products without a discoverable EAN are excluded (logged as warnings).
    max_products: stop early once this many products with EANs are found (None = no limit).
    """
    _assert_safe_url(webshop_url)
    logger.info(f"Starting product discovery for {webshop_url}")

    # Step 1: Try sitemap.xml
    product_urls = _get_urls_from_sitemap(webshop_url)

    # Step 2: Fallback to BFS crawl
    if not product_urls:
        logger.info("No sitemap found — falling back to BFS crawl")
        product_urls = _crawl_for_product_urls(webshop_url, max_pages, rate_limit_seconds)

    logger.info(f"Found {len(product_urls)} candidate product URLs")

    # Step 3: Extract product data from each URL
    products = []
    seen_eans = set()

    for i, url in enumerate(product_urls):
        if max_products and len(products) >= max_products:
            logger.info(f"Reached limit of {max_products} products — stopping early")
            break

        time.sleep(rate_limit_seconds)
        product = extract_product_from_page(url)

        if product is None:
            continue

        ean = product.get("ean")
        if not ean:
            logger.debug(f"No EAN found for {url}")
            continue

        if ean in seen_eans:
            continue
        seen_eans.add(ean)

        products.append(product)
        if (i + 1) % 100 == 0:
            logger.info(f"  Extracted {len(products)} products from {i + 1} pages")

    logger.info(f"Discovery complete: {len(products)} products with EANs")
    return products


def extract_product_from_page(url: str) -> Optional[dict]:
    """
    Extract product data from a single product page URL.
    Returns {ean, product_name, client_price, client_currency, client_url} or None.
    """
    try:
        resp = _session.get(url, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        logger.debug(f"Failed to fetch {url}: {e}")
        return None

    soup = BeautifulSoup(resp.text, "lxml")

    # Try JSON-LD first
    product = _extract_from_jsonld(soup, url)
    if product:
        return product

    # Fallback: CSS heuristics
    return _extract_from_css_heuristics(soup, url)


# ── Sitemap parser ─────────────────────────────────────────────────────────────

def _get_urls_from_sitemap(webshop_url: str) -> list[str]:
    """Parse sitemap.xml and return product page URLs."""
    sitemap_url = webshop_url.rstrip("/") + "/sitemap.xml"
    try:
        resp = _session.get(sitemap_url, timeout=10)
        resp.raise_for_status()
    except Exception:
        return []

    soup = BeautifulSoup(resp.text, "lxml-xml")

    # Handle sitemap index (links to other sitemaps)
    sitemaps = soup.find_all("sitemap")
    if sitemaps:
        all_urls = []
        for sm in sitemaps:
            loc = sm.find("loc")
            if loc:
                child_urls = _parse_sitemap_url(loc.text.strip())
                all_urls.extend(child_urls)
        return _filter_product_urls(all_urls)

    # Regular sitemap
    locs = [loc.text.strip() for loc in soup.find_all("loc")]
    return _filter_product_urls(locs)


def _parse_sitemap_url(sitemap_url: str) -> list[str]:
    """Fetch and parse a single sitemap URL, return all loc values."""
    try:
        resp = _session.get(sitemap_url, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml-xml")
        return [loc.text.strip() for loc in soup.find_all("loc")]
    except Exception:
        return []


def _filter_product_urls(urls: list[str]) -> list[str]:
    """
    Filter URL list to likely product pages.
    Primary: match known product URL patterns.
    Fallback: if primary yields <50 results from a large sitemap (>200 URLs),
    treat any URL with 3+ path segments as a product candidate.
    This handles shops that use deep category/slug paths without numeric IDs.
    """
    pattern = re.compile("|".join(PRODUCT_URL_PATTERNS), re.IGNORECASE)
    primary = [u for u in urls if pattern.search(u)]

    if len(primary) >= 50 or len(urls) < 200:
        return primary

    # Fallback: depth-based detection (3+ path segments)
    logger.info(
        f"Pattern filter returned only {len(primary)} URLs from {len(urls)} — "
        "falling back to depth-based product detection (3+ path segments)"
    )
    depth_based = []
    for u in urls:
        path = urlparse(u).path.strip("/")
        if path.count("/") >= 2:  # 3+ segments
            depth_based.append(u)
    logger.info(f"Depth-based filter: {len(depth_based)} candidate product URLs")
    return depth_based


# ── BFS crawler ───────────────────────────────────────────────────────────────

def _crawl_for_product_urls(
    start_url: str,
    max_pages: int,
    delay: float,
) -> list[str]:
    """BFS crawl from start_url, collect internal links matching product patterns."""
    base = urlparse(start_url)
    visited = set()
    product_urls = []
    product_url_set = set()  # PERF-F: O(1) dedup companion
    queue = deque([start_url])
    product_pattern = re.compile("|".join(PRODUCT_URL_PATTERNS), re.IGNORECASE)

    while queue and len(visited) < max_pages:
        url = queue.popleft()
        if url in visited:
            continue
        visited.add(url)
        time.sleep(delay)

        try:
            resp = _session.get(url, timeout=10)
            resp.raise_for_status()
        except Exception:
            continue

        if "text/html" not in resp.headers.get("Content-Type", ""):
            continue

        soup = BeautifulSoup(resp.text, "lxml")

        for a in soup.find_all("a", href=True):
            href = urljoin(url, a["href"])
            parsed = urlparse(href)

            # Stay on same domain
            if parsed.netloc != base.netloc:
                continue
            # Ignore anchors, query-only, non-http
            if parsed.scheme not in ("http", "https"):
                continue
            clean = href.split("#")[0]
            if clean in visited:
                continue

            if product_pattern.search(clean):
                if clean not in product_url_set:
                    product_urls.append(clean)
                    product_url_set.add(clean)
            else:
                queue.append(clean)

    return product_urls


# ── JSON-LD extractor ─────────────────────────────────────────────────────────

def _extract_from_jsonld(soup: BeautifulSoup, url: str) -> Optional[dict]:
    """Extract product data from JSON-LD script tags."""
    for script in soup.find_all("script", type="application/ld+json"):
        if len(script.string or "") > 1_000_000:
            continue  # SEC-E: skip excessively large JSON-LD blocks
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue

        # Handle @graph arrays
        if isinstance(data, dict) and "@graph" in data:
            data = data["@graph"]

        items = data if isinstance(data, list) else [data]
        items = items[:200]  # SEC-E: bound items list

        for item in items:
            if not isinstance(item, dict):
                continue
            item_type = item.get("@type", "")
            if isinstance(item_type, list):
                item_type = " ".join(item_type)
            if "Product" not in item_type:
                continue

            ean = _extract_ean(item)
            name = item.get("name", "")
            price, currency = _extract_price(item)

            if ean:
                return {
                    "ean": ean,
                    "product_name": name,
                    "client_price": price,
                    "client_currency": currency,
                    "client_url": url,
                }

    return None


def _extract_ean(item: dict) -> Optional[str]:
    """Extract EAN using priority chain: gtin13 → gtin14 → gtin8 → gtin → sku."""
    for field in EAN_FIELDS:
        val = item.get(field)
        if val and str(val).strip():
            ean = str(val).strip()
            if field == "sku":
                logger.debug(f"Using SKU as EAN proxy: {ean}")
            return ean
    return None


def _extract_price(item: dict) -> tuple[Optional[float], str]:
    """Extract price and currency from a JSON-LD Product item."""
    offers = item.get("offers", {})
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    if not isinstance(offers, dict):
        offers = {}  # CODE-A: guard offers=[None]

    price = offers.get("price") or offers.get("lowPrice")
    currency = offers.get("priceCurrency", "")

    if price is not None:
        try:
            return float(str(price).replace(",", ".")), currency
        except (ValueError, TypeError):
            pass

    return None, currency


# ── CSS heuristic fallback ────────────────────────────────────────────────────

# Common selectors for price and EAN fields across popular platforms
PRICE_SELECTORS = [
    '[itemprop="price"]',
    ".price",
    ".product-price",
    ".woocommerce-Price-amount",
    '[class*="price"]',
]

EAN_SELECTORS = [
    '[itemprop="gtin13"]',
    '[itemprop="gtin"]',
    '[itemprop="sku"]',
    '[class*="ean"]',
    '[class*="barcode"]',
    '[data-ean]',
]


def _extract_from_css_heuristics(soup: BeautifulSoup, url: str) -> Optional[dict]:
    """Fallback: extract product data using common CSS selectors."""
    ean = None
    for sel in EAN_SELECTORS:
        el = soup.select_one(sel)
        if el:
            val = el.get("content") or el.get("data-ean") or el.get_text(strip=True)
            if val and re.match(r"^\d{8,14}$", val.strip()):
                ean = val.strip()
                logger.debug(f"CSS heuristic EAN found via '{sel}': {ean}")
                break

    if not ean:
        return None

    price = None
    currency = ""
    for sel in PRICE_SELECTORS:
        el = soup.select_one(sel)
        if el:
            raw = el.get("content") or el.get_text(strip=True)
            # Strip currency symbols, spaces, normalize decimal
            cleaned = re.sub(r"[^\d.,]", "", raw).replace(",", ".")
            try:
                price = float(cleaned)
                break
            except ValueError:
                continue

    name_el = (
        soup.find("h1")
        or soup.select_one('[itemprop="name"]')
        or soup.select_one(".product-title")
    )
    name = name_el.get_text(strip=True) if name_el else ""

    return {
        "ean": ean,
        "product_name": name,
        "client_price": price,
        "client_currency": currency,
        "client_url": url,
    }
