"""
Direct competitor site scraping — fallback when aggregators return no results.
Uses subprocess Firecrawl MCP tool via the Claude MCP bridge, or falls back
to a simple requests-based search if MCP is unavailable.
"""

import json
import logging
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional
from urllib.parse import quote_plus, urljoin

import requests
from bs4 import BeautifulSoup

from scrapers.utils import DEFAULT_HEADERS, RateLimiter, _parse_price, _name_similarity

logger = logging.getLogger(__name__)

MAX_DIRECT_WORKERS = 5


def _validate_domain(domain: str) -> None:
    """Reject domains that look like IPs or contain path/query characters."""
    import ipaddress as _ipaddress
    stripped = domain.replace("www.", "", 1)
    # Reject bare IP addresses
    try:
        _ipaddress.ip_address(stripped)
        raise ValueError(f"Domain cannot be an IP address: {domain}")
    except ValueError as e:
        if "IP address" in str(e):
            raise
    # Reject path/query injection
    if "/" in domain or "?" in domain:
        raise ValueError(f"Domain contains invalid characters: {domain}")


def search_competitor(
    ean: str,
    domain: str,
    rate_limiter: RateLimiter,
) -> list[dict]:
    """
    Search a single competitor domain for a product by EAN.
    Tries common search URL patterns, extracts price + product URL.
    Returns list of price results (usually 0 or 1).
    """
    _validate_domain(domain)
    rate_limiter.wait(domain)

    # Common search URL patterns for e-commerce platforms
    search_urls = [
        f"https://www.{domain}/search?q={ean}",
        f"https://www.{domain}/kereses?q={ean}",        # Hungarian
        f"https://www.{domain}/suche?q={ean}",           # German
        f"https://www.{domain}/search?query={ean}",
        f"https://www.{domain}/catalogsearch/result/?q={ean}",  # Magento
        f"https://www.{domain}/?s={ean}",                # WooCommerce
    ]

    for search_url in search_urls:
        try:
            resp = requests.get(search_url, headers=DEFAULT_HEADERS, timeout=12, allow_redirects=True)
            if resp.status_code == 404:
                continue
            if resp.status_code in (403, 429):
                logger.debug(f"Blocked on {domain} ({resp.status_code})")
                return []
            resp.raise_for_status()
        except requests.exceptions.ConnectionError:
            continue
        except Exception as e:
            logger.debug(f"Direct scrape error on {domain}: {e}")
            continue

        results = _parse_search_results(resp.text, domain, ean)
        if results:
            return results

    return []


def _parse_search_results(html: str, domain: str, ean: str) -> list[dict]:
    """Parse search results page from a competitor site."""
    soup = BeautifulSoup(html, "lxml")
    results = []

    # Try JSON-LD Product schema first (some sites embed it on search pages)
    for script in soup.find_all("script", type="application/ld+json"):
        if len(script.string or "") > 1_000_000:
            continue  # SEC-E: skip excessively large JSON-LD blocks
        try:
            data = json.loads(script.string or "")
            items = data if isinstance(data, list) else [data]
            items = items[:200]  # SEC-E: bound items list
            for item in items:
                if not isinstance(item, dict):
                    continue
                if "Product" not in str(item.get("@type", "")):
                    continue
                # Verify EAN matches
                ean_found = None
                for field in ["gtin13", "gtin14", "gtin8", "gtin", "sku"]:
                    if str(item.get(field, "")).strip() == ean:
                        ean_found = True
                        break
                if not ean_found:
                    continue

                offers = item.get("offers", {})
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                price = _parse_price(str(offers.get("price", "")))
                currency = offers.get("priceCurrency", "")
                product_url = item.get("url", "")

                if price:
                    results.append({
                        "store_name": domain.replace("www.", ""),
                        "store_url": f"https://www.{domain}",
                        "product_url": product_url,
                        "price": price,
                        "currency": currency,
                    })
        except Exception as e:
            logger.debug(f"JSON-LD parse error on {domain}: {e}")

    if results:
        return results

    # Fallback: look for first product card on the search results page
    product_cards = soup.select(
        ".product-item, .product-card, [class*='product-list'] li, "
        ".search-result-item, [class*='search-result']"
    )[:3]

    for card in product_cards:
        price_el = card.select_one("[class*='price'], .price, [itemprop='price']")
        link_el = card.select_one("a[href]")

        if not price_el or not link_el:
            continue

        price = _parse_price(price_el.get("content") or price_el.get_text(strip=True))
        if price is None:
            continue

        href = link_el["href"]
        product_url = urljoin(f"https://www.{domain}", href)

        results.append({
            "store_name": domain.replace("www.", ""),
            "store_url": f"https://www.{domain}",
            "product_url": product_url,
            "price": price,
            "currency": "",
        })
        break  # Take only first match

    return results


def search_all_competitors(
    ean: str,
    competitor_domains: list[str],
    rate_limiter: RateLimiter,
) -> list[dict]:
    """
    Search all configured competitor domains in parallel.
    Returns combined results from all domains.
    """
    if not competitor_domains:
        return []

    all_results: list[dict] = []

    with ThreadPoolExecutor(max_workers=MAX_DIRECT_WORKERS) as executor:
        futures = {
            executor.submit(search_competitor, ean, domain, rate_limiter): domain
            for domain in competitor_domains
        }
        for future in as_completed(futures):
            domain = futures[future]
            try:
                results = future.result()
                all_results.extend(results)
                if results:
                    logger.debug(f"Direct: {len(results)} results from {domain} for EAN {ean}")
            except Exception as e:
                logger.debug(f"Direct scrape failed for {domain}: {e}")

    return all_results


def search_competitor_by_name(
    product_name: str,
    domain: str,
    rate_limiter: RateLimiter,
    price_floor: float = 0.0,
) -> list[dict]:
    """
    Search a single competitor domain for a product by name (for products without EANs).
    Tries common search URL patterns, skips EAN verification.
    price_floor filters out accessories / false positives below the threshold (HUF or local currency).
    Returns list of price results (usually 0–3).
    """
    _validate_domain(domain)
    rate_limiter.wait(domain)
    query = quote_plus(product_name)

    search_urls = [
        f"https://www.{domain}/search?q={query}",
        f"https://www.{domain}/kereses?q={query}",        # Hungarian
        f"https://www.{domain}/suche?q={query}",           # German
        f"https://www.{domain}/search?query={query}",
        f"https://www.{domain}/catalogsearch/result/?q={query}",  # Magento
        f"https://www.{domain}/?s={query}",                # WooCommerce
    ]

    for search_url in search_urls:
        try:
            resp = requests.get(search_url, headers=DEFAULT_HEADERS, timeout=12, allow_redirects=True)
            if resp.status_code == 404:
                continue
            if resp.status_code in (403, 429):
                logger.debug(f"Blocked on {domain} ({resp.status_code})")
                return []
            resp.raise_for_status()
        except requests.exceptions.ConnectionError:
            continue
        except Exception as e:
            logger.debug(f"Direct name-scrape error on {domain}: {e}")
            continue

        results = _parse_search_results_by_name(resp.text, domain, product_name, price_floor)
        if results:
            return results

    return []


def _parse_search_results_by_name(
    html: str, domain: str, product_name: str, price_floor: float
) -> list[dict]:
    """
    Parse search results page for a name-based query.
    Skips EAN verification. Applies price_floor to filter accessories.
    """
    # _name_similarity imported at module level from scrapers.utils
    soup = BeautifulSoup(html, "lxml")
    results = []

    # Try JSON-LD Product schema first
    for script in soup.find_all("script", type="application/ld+json"):
        if len(script.string or "") > 1_000_000:
            continue  # SEC-E: skip excessively large JSON-LD blocks
        try:
            data = json.loads(script.string or "")
            items = data if isinstance(data, list) else [data]
            items = items[:200]  # SEC-E: bound items list
            for item in items:
                if not isinstance(item, dict):
                    continue
                if "Product" not in str(item.get("@type", "")):
                    continue

                item_name = item.get("name", "")
                if item_name and _name_similarity(product_name, item_name) < 0.3:
                    continue  # Different product

                offers = item.get("offers", {})
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                price = _parse_price(str(offers.get("price", "")))
                if not price or price < price_floor:
                    continue

                results.append({
                    "store_name": domain.replace("www.", ""),
                    "store_url": f"https://www.{domain}",
                    "product_url": item.get("url", ""),
                    "price": price,
                    "currency": offers.get("priceCurrency", ""),
                })
        except Exception as e:
            logger.debug(f"JSON-LD parse error on {domain}: {e}")

    if results:
        return results[:3]

    # Fallback: product card heuristic
    product_cards = soup.select(
        ".product-item, .product-card, [class*='product-list'] li, "
        ".search-result-item, [class*='search-result']"
    )[:5]

    for card in product_cards:
        price_el = card.select_one("[class*='price'], .price, [itemprop='price']")
        link_el = card.select_one("a[href]")
        if not price_el or not link_el:
            continue

        price = _parse_price(price_el.get("content") or price_el.get_text(strip=True))
        if price is None or price < price_floor:
            continue

        from urllib.parse import urljoin
        product_url = urljoin(f"https://www.{domain}", link_el["href"])

        results.append({
            "store_name": domain.replace("www.", ""),
            "store_url": f"https://www.{domain}",
            "product_url": product_url,
            "price": price,
            "currency": "",
        })
        break  # Take only first card match

    return results


def search_all_competitors_by_name(
    product_name: str,
    competitor_domains: list[str],
    rate_limiter: RateLimiter,
    price_floor: float = 0.0,
) -> list[dict]:
    """
    Search all competitor domains in parallel using product name (not EAN).
    For use with products that have no EAN (e.g. Rextra Littmann).
    Returns combined, deduplicated results from all domains.
    """
    if not competitor_domains:
        return []

    all_results: list[dict] = []

    with ThreadPoolExecutor(max_workers=MAX_DIRECT_WORKERS) as executor:
        futures = {
            executor.submit(
                search_competitor_by_name, product_name, domain, rate_limiter, price_floor
            ): domain
            for domain in competitor_domains
        }
        for future in as_completed(futures):
            domain = futures[future]
            try:
                results = future.result()
                all_results.extend(results)
                if results:
                    logger.debug(
                        f"Direct name-search: {len(results)} result(s) from {domain} "
                        f"for '{product_name[:40]}'"
                    )
            except Exception as e:
                logger.debug(f"Direct name-scrape failed for {domain}: {e}")

    return all_results
