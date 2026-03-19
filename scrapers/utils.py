"""
Shared utilities for price-scraper scrapers.
Extracted from aggregator.py to eliminate private-symbol coupling (ARCH-A)
and deduplication triplication (ARCH-G).
"""

import difflib
import re
import threading
import time
from typing import Optional
from urllib.parse import urlparse, urljoin, quote, quote_plus  # noqa: F401 — re-exported


DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "hu-HU,hu;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


class RateLimiter:
    """Per-domain token bucket rate limiter. Thread-safe."""

    def __init__(self, delay_seconds: float = 2.0):
        self._delay = delay_seconds
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, domain: str):
        with self._lock:
            now = time.time()
            last = self._last.get(domain, 0)
            elapsed = now - last
            if elapsed < self._delay:
                time.sleep(self._delay - elapsed)
            self._last[domain] = time.time()


def parse_price(text: str) -> Optional[float]:
    """Parse a price string to float. Handles various formats."""
    if not text:
        return None
    # Remove currency symbols, spaces, normalize decimal separator
    cleaned = re.sub(r"[^\d.,]", "", str(text).strip())
    if not cleaned:
        return None
    # Handle "1.234,56" (European) and "1,234.56" (US) formats
    if re.search(r"\d{1,3}\.\d{3},\d{2}", cleaned):
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif re.search(r"\d{1,3},\d{3}\.\d{2}", cleaned):
        cleaned = cleaned.replace(",", "")
    elif "," in cleaned and "." not in cleaned:
        cleaned = cleaned.replace(",", ".")
    elif "." in cleaned and "," in cleaned:
        # Last separator is decimal
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "." in cleaned and "," not in cleaned:
        # Unambiguous EU thousands separator: "1.234" or "12.345.678" (no decimal part)
        if re.match(r"^\d{1,3}(\.\d{3})+$", cleaned):
            cleaned = cleaned.replace(".", "")
        # else: treat as decimal (e.g. "12.99")
    try:
        return float(cleaned)
    except ValueError:
        return None


# Backward-compatible alias
_parse_price = parse_price


def name_similarity(a: str, b: str) -> float:
    """Case-insensitive token-sorted similarity between two product name strings."""
    a_norm = " ".join(sorted(a.lower().split()))
    b_norm = " ".join(sorted(b.lower().split()))
    return difflib.SequenceMatcher(None, a_norm, b_norm).ratio()


# Backward-compatible alias
_name_similarity = name_similarity


def extract_base_url(url: str) -> str:
    """Extract scheme + netloc from a URL. Returns '' for relative URLs."""
    parsed = urlparse(url)
    if parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return ""


# Backward-compatible alias
_extract_base_url = extract_base_url


def dedupe_results(results: list[dict]) -> list[dict]:
    """Deduplicate by (store_name, price), keep first occurrence. Sort by price asc."""
    seen = set()
    deduped = []
    for r in results:
        key = (r.get("store_name", "").lower(), r.get("price"))
        if key not in seen:
            seen.add(key)
            deduped.append(r)
    return sorted(deduped, key=lambda x: x.get("price") or float("inf"))
