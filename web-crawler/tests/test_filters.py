"""Unit tests for URLFilter."""

import pytest

from web_crawler.filters import URLFilter


class TestURLFilterScheme:
    """Test scheme rejection (ftp, mailto)."""

    def test_rejects_ftp(self):
        f = URLFilter()
        assert not f.accept("ftp://example.com/file.txt")

    def test_rejects_mailto(self):
        f = URLFilter()
        assert not f.accept("mailto:user@example.com")

    def test_accepts_http(self):
        f = URLFilter()
        assert f.accept("http://example.com/page")

    def test_accepts_https(self):
        f = URLFilter()
        assert f.accept("https://example.com/page")


class TestURLFilterExtensionBlacklist:
    """Test extension blacklist (.jpg, .pdf)."""

    def test_rejects_jpg(self):
        f = URLFilter(extension_blacklist=[".jpg"])
        assert not f.accept("http://example.com/image.jpg")

    def test_rejects_pdf(self):
        f = URLFilter(extension_blacklist=[".pdf"])
        assert not f.accept("http://example.com/doc.pdf")

    def test_accepts_html(self):
        f = URLFilter(extension_blacklist=[".jpg", ".pdf"])
        assert f.accept("http://example.com/page.html")

    def test_case_insensitive(self):
        f = URLFilter(extension_blacklist=[".jpg"])
        assert not f.accept("http://example.com/image.JPG")


class TestURLFilterDomainWhitelist:
    """Test domain whitelist."""

    def test_accepts_whitelisted_domain(self):
        f = URLFilter(domain_whitelist=["example.com"])
        assert f.accept("http://example.com/page")

    def test_rejects_non_whitelisted_domain(self):
        f = URLFilter(domain_whitelist=["example.com"])
        assert not f.accept("http://other.com/page")

    def test_no_whitelist_accepts_all(self):
        f = URLFilter(domain_whitelist=None)
        assert f.accept("http://anything.com/page")


class TestURLFilterDomainBlacklist:
    """Test domain blacklist."""

    def test_rejects_blacklisted_domain(self):
        f = URLFilter(domain_blacklist=["bad.com"])
        assert not f.accept("http://bad.com/page")

    def test_accepts_non_blacklisted_domain(self):
        f = URLFilter(domain_blacklist=["bad.com"])
        assert f.accept("http://good.com/page")


class TestURLFilterMaxLength:
    """Test max URL length."""

    def test_rejects_long_url(self):
        f = URLFilter(max_url_length=50)
        long_url = "http://example.com/" + "a" * 50
        assert not f.accept(long_url)

    def test_accepts_short_url(self):
        f = URLFilter(max_url_length=100)
        assert f.accept("http://example.com/page")


class TestURLFilterCustomFilters:
    """Test custom filter functions."""

    def test_custom_filter_rejects(self):
        f = URLFilter(custom_filters=[lambda url: "allowed" in url])
        assert not f.accept("http://example.com/blocked")

    def test_custom_filter_accepts(self):
        f = URLFilter(custom_filters=[lambda url: "allowed" in url])
        assert f.accept("http://example.com/allowed")

    def test_multiple_custom_filters_all_must_pass(self):
        f = URLFilter(
            custom_filters=[
                lambda url: "a" in url,
                lambda url: "b" in url,
            ]
        )
        assert not f.accept("http://example.com/a")
        assert f.accept("http://example.com/ab")


class TestURLFilterWhitelistBlacklistInteraction:
    """Test whitelist + blacklist interaction."""

    def test_whitelist_and_blacklist_both_applied(self):
        # Domain in whitelist AND blacklist → rejected (blacklist wins)
        f = URLFilter(
            domain_whitelist=["example.com"],
            domain_blacklist=["example.com"],
        )
        assert not f.accept("http://example.com/page")

    def test_whitelist_passes_blacklist_rejects(self):
        f = URLFilter(
            domain_whitelist=["example.com", "bad.com"],
            domain_blacklist=["bad.com"],
        )
        assert f.accept("http://example.com/page")
        assert not f.accept("http://bad.com/page")
