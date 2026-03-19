# Adding a New Country

## Step 1: Add Country Config

Edit `config/sites.json`:

```json
{
  "SE": {
    "aggregators": ["google_shopping"],
    "competitors": ["competitor1.se", "competitor2.se"],
    "currency": "SEK",
    "country_code": "SE",
    "language": "sv",
    "crawl_rate_limit": 1.0,
    "aggregator_rate_limit": 2.0
  }
}
```

### Config Fields

| Field | Required | Description |
|-------|----------|-------------|
| `aggregators` | Yes | List of aggregator keys: `"arukereso"`, `"google_shopping"`, `"idealo"` |
| `competitors` | Yes | Direct competitor domains for fallback (can be empty `[]`) |
| `currency` | Yes | ISO 4217 currency code |
| `country_code` | Yes | ISO 3166-1 alpha-2 code (used for Google Shopping `gl=` param) |
| `language` | Yes | Language code for Accept-Language header |
| `crawl_rate_limit` | No | Seconds between webshop crawl requests (default: 2) |
| `aggregator_rate_limit` | No | Seconds between aggregator requests (default: 2) |
| `idealo_tld` | No | Required if using idealo (e.g. `"de"`, `"at"`, `"fr"`) |

## Step 2: Add Aggregator (if new)

If your country has a price comparison site not yet supported, add a scraper:

### 1. Create the search function in `scrapers/aggregator.py`:

```python
def search_new_aggregator(ean: str, rate_limiter: RateLimiter) -> list[dict]:
    """Search newsite.se for a product by EAN."""
    domain = "newsite.se"
    rate_limiter.wait(domain)

    url = f"https://www.newsite.se/search?q={ean}"
    try:
        resp = requests.get(url, headers=DEFAULT_HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        logger.warning(f"newsite.se failed for EAN {ean}: {e}")
        return []

    return _parse_new_aggregator(resp.text, ean)
```

### 2. Add the parser function:

```python
def _parse_new_aggregator(html: str, ean: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    results = []
    # Parse store offers...
    for offer in soup.select(".offer-item"):
        store = offer.select_one(".store-name")
        price_el = offer.select_one(".price")
        if not store or not price_el:
            continue
        price = _parse_price(price_el.get_text(strip=True))
        if price is None:
            continue
        results.append({
            "store_name": store.get_text(strip=True),
            "store_url": "",
            "product_url": "",
            "price": price,
            "currency": "SEK",
        })
    return results
```

### 3. Register in the dispatcher (`search_aggregators`):

```python
elif agg == "newsite":
    results = search_new_aggregator(ean, rate_limiter)
```

### 4. Update the currency map if using idealo:

In `_extract_idealo_offer()`, add the TLD → currency mapping:
```python
currency = {
    ...,
    "se": "SEK",
}.get(tld, "")
```

## Step 3: Add Competitors

List known competitor domains in the `competitors` array. These are searched as a fallback when aggregators return < 2 results.

The direct scraper tries common search URL patterns:
- `/search?q={EAN}`
- `/kereses?q={EAN}` (Hungarian)
- `/suche?q={EAN}` (German)
- `/catalogsearch/result/?q={EAN}` (Magento)
- `/?s={EAN}` (WooCommerce)

If the competitor uses a different search URL pattern, you may need to add it to `scrapers/direct.py` `search_urls` list.

## Step 4: Test

```bash
# Dry run to verify discovery works
python3 price_scraper.py \
  --webshop https://www.example.se \
  --sheet test \
  --country SE \
  --dry-run --limit 5

# Run tests
python3 -m pytest tests/ -v
```
