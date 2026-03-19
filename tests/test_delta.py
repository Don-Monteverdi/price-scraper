"""Tests for _calculate_delta — empty currency, cross-currency, valid cases."""
import pytest
from pipeline.sheet_sync import _calculate_delta


def test_valid_delta_positive():
    """Client overpriced by 20%."""
    result = _calculate_delta(120.0, 100.0, "HUF", "HUF")
    assert result == "+20.0%"


def test_valid_delta_negative():
    """Client underpriced by 50%."""
    result = _calculate_delta(50.0, 100.0, "EUR", "EUR")
    assert result == "-50.0%"


def test_valid_delta_zero():
    """Same price = 0%."""
    result = _calculate_delta(100.0, 100.0, "HUF", "HUF")
    assert result == "+0.0%" or result == "0.0%"


def test_empty_client_currency():
    """Empty client currency should return dash."""
    result = _calculate_delta(100.0, 80.0, "", "HUF")
    assert result == "\u2014"


def test_empty_cheapest_currency():
    """Empty cheapest currency should return dash."""
    result = _calculate_delta(100.0, 80.0, "HUF", "")
    assert result == "\u2014"


def test_both_currencies_empty():
    """Both currencies empty should return dash."""
    result = _calculate_delta(100.0, 80.0, "", "")
    assert result == "\u2014"


def test_cross_currency():
    """Different currencies should return dash (no conversion)."""
    result = _calculate_delta(100.0, 80.0, "HUF", "EUR")
    assert result == "\u2014"


def test_missing_client_price():
    """None client price should return dash."""
    result = _calculate_delta(None, 80.0, "HUF", "HUF")
    assert result == "\u2014"


def test_missing_cheapest_price():
    """None cheapest price should return dash."""
    result = _calculate_delta(100.0, None, "HUF", "HUF")
    assert result == "\u2014"


def test_zero_cheapest_price():
    """Zero cheapest price (division by zero) should return dash."""
    result = _calculate_delta(100.0, 0, "HUF", "HUF")
    assert result == "\u2014"


def test_unreliable_match():
    """Unreliable match should return warning string."""
    result = _calculate_delta(100.0, 80.0, "HUF", "HUF", match_reliable=False)
    assert "megb\u00edzhatatlan" in result.lower() or "elt\u00e9r\u0151" in result.lower()
