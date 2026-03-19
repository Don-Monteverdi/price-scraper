"""Tests for parse_price — covers EU, US, HUF thousands, malformed, empty."""
import pytest
from scrapers.utils import parse_price


@pytest.mark.parametrize(
    "input_text, expected",
    [
        # European format: 1.234,56
        ("1.234,56 EUR", 1234.56),
        # US format: 1,234.56
        ("$1,234.56", 1234.56),
        # HUF thousands separator (STOP-3): "63.488" = 63488
        ("63.488", 63488.0),
        ("1.234", 1234.0),
        ("12.345.678", 12345678.0),
        # Simple decimal
        ("12.99", 12.99),
        # Comma as decimal (EU single price)
        ("12,99", 12.99),
        # Plain integer
        ("500", 500.0),
        # Malformed / empty
        ("", None),
        ("N/A", None),
        # Currency symbols stripped
        ("63 488 Ft", 63488.0),
    ],
)
def test_parse_price(input_text: str, expected):
    assert parse_price(input_text) == expected
