"""Tests for shared utilities — dedupe, name_similarity, extract_base_url."""
import pytest
from scrapers.utils import dedupe_results, name_similarity, extract_base_url


class TestDedupeResults:
    def test_removes_duplicates(self):
        results = [
            {"store_name": "Shop A", "price": 100},
            {"store_name": "Shop A", "price": 100},
            {"store_name": "Shop B", "price": 200},
        ]
        deduped = dedupe_results(results)
        assert len(deduped) == 2

    def test_keeps_different_prices(self):
        results = [
            {"store_name": "Shop A", "price": 100},
            {"store_name": "Shop A", "price": 150},
        ]
        deduped = dedupe_results(results)
        assert len(deduped) == 2

    def test_sorts_by_price_ascending(self):
        results = [
            {"store_name": "Expensive", "price": 500},
            {"store_name": "Cheap", "price": 50},
            {"store_name": "Mid", "price": 200},
        ]
        deduped = dedupe_results(results)
        prices = [r["price"] for r in deduped]
        assert prices == [50, 200, 500]

    def test_empty_input(self):
        assert dedupe_results([]) == []

    def test_case_insensitive_store_name(self):
        results = [
            {"store_name": "Shop A", "price": 100},
            {"store_name": "shop a", "price": 100},
        ]
        deduped = dedupe_results(results)
        assert len(deduped) == 1


class TestNameSimilarity:
    def test_identical_names(self):
        assert name_similarity("Littmann Classic III", "Littmann Classic III") == 1.0

    def test_case_insensitive(self):
        score = name_similarity("littmann classic", "LITTMANN CLASSIC")
        assert score == 1.0

    def test_partial_match(self):
        score = name_similarity(
            "Littmann Classic III Stethoscope",
            "Littmann Classic III"
        )
        assert 0.5 < score < 1.0

    def test_completely_different(self):
        score = name_similarity("Littmann Classic", "Samsung Galaxy S24")
        assert score < 0.4

    def test_word_order_independent(self):
        """Token-sorted similarity should handle reordered words."""
        score = name_similarity("Classic Littmann III", "Littmann Classic III")
        assert score == 1.0


class TestExtractBaseUrl:
    def test_full_url(self):
        assert extract_base_url("https://www.example.com/path/page") == "https://www.example.com"

    def test_with_port(self):
        assert extract_base_url("http://localhost:8080/api") == "http://localhost:8080"

    def test_relative_url(self):
        assert extract_base_url("/path/to/page") == ""

    def test_empty_string(self):
        assert extract_base_url("") == ""

    def test_just_domain(self):
        assert extract_base_url("https://example.com") == "https://example.com"
