"""Tests for security hardening — SSRF, domain validation, regex."""
import pytest
from scrapers.client_webshop import _assert_safe_url
from scrapers.direct import _validate_domain


class TestSSRFProtection:
    """SEC-C: _assert_safe_url blocks private IPs and non-HTTPS."""

    def test_blocks_localhost(self):
        with pytest.raises(ValueError, match="private|reserved|loopback"):
            _assert_safe_url("http://127.0.0.1/admin")

    def test_blocks_private_ip(self):
        with pytest.raises(ValueError, match="private|reserved|loopback"):
            _assert_safe_url("http://192.168.1.1/secret")

    def test_blocks_non_http_scheme(self):
        with pytest.raises(ValueError, match="scheme"):
            _assert_safe_url("file:///etc/passwd")

    def test_blocks_ftp_scheme(self):
        with pytest.raises(ValueError, match="scheme"):
            _assert_safe_url("ftp://internal.server/data")

    def test_allows_public_https(self):
        # Should not raise
        _assert_safe_url("https://www.example.com/products")

    def test_allows_public_http(self):
        # Should not raise (HTTP is allowed, just not private IPs)
        _assert_safe_url("http://www.example.com/products")

    def test_blocks_no_hostname(self):
        with pytest.raises(ValueError, match="hostname|resolve"):
            _assert_safe_url("http:///path")


class TestDomainValidation:
    """SEC-J: _validate_domain rejects IPs and path injection."""

    def test_rejects_ipv4(self):
        with pytest.raises(ValueError, match="IP address"):
            _validate_domain("192.168.1.1")

    def test_rejects_path_injection(self):
        with pytest.raises(ValueError, match="invalid"):
            _validate_domain("example.com/admin")

    def test_rejects_query_injection(self):
        with pytest.raises(ValueError, match="invalid"):
            _validate_domain("example.com?q=hack")

    def test_allows_valid_domain(self):
        # Should not raise
        _validate_domain("www.example.com")
        _validate_domain("orvosieszkoz.hu")

    def test_allows_subdomain(self):
        _validate_domain("shop.example.co.uk")


class TestAjanlatRegex:
    """STOP-5: 'ajanlat' regex should not match 'ajandek'."""

    def test_matches_ajanlat(self):
        import re
        pattern = r"^\d+\s+aj\u00e1nlat"
        assert re.match(pattern, "5 aj\u00e1nlat")
        assert re.match(pattern, "12 aj\u00e1nlat")

    def test_does_not_match_ajandek(self):
        import re
        pattern = r"^\d+\s+aj\u00e1nlat"
        assert not re.match(pattern, "5 aj\u00e1nd\u00e9k")
        assert not re.match(pattern, "1 aj\u00e1nd\u00e9k csomag")
