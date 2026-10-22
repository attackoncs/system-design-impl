"""Unit tests for deduplication (ContentSeen and URLSeen)."""

import web_crawler.dedup


class TestContentSeenFingerprint:
    """Test ContentSeen fingerprint computation (deterministic)."""

    def test_deterministic_fingerprint(self):
        cs = web_crawler.dedup.ContentSeen()
        content = b"Hello, World!"
        fp1 = cs.compute_fingerprint(content)
        fp2 = cs.compute_fingerprint(content)
        assert fp1 == fp2

    def test_different_content_different_fingerprint(self):
        cs = web_crawler.dedup.ContentSeen()
        fp1 = cs.compute_fingerprint(b"content A")
        fp2 = cs.compute_fingerprint(b"content B")
        assert fp1 != fp2

    def test_fingerprint_is_hex_string(self):
        cs = web_crawler.dedup.ContentSeen()
        fp = cs.compute_fingerprint(b"test")
        assert all(c in "0123456789abcdef" for c in fp)
        assert len(fp) == 64  # SHA-256 hex digest length


class TestContentSeenIsSeen:
    """Test ContentSeen is_seen/add."""

    def test_not_seen_initially(self):
        cs = web_crawler.dedup.ContentSeen()
        fp = cs.compute_fingerprint(b"content")
        assert not cs.is_seen(fp)

    def test_seen_after_add(self):
        cs = web_crawler.dedup.ContentSeen()
        fp = cs.compute_fingerprint(b"content")
        cs.add(fp)
        assert cs.is_seen(fp)

    def test_different_fingerprint_not_seen(self):
        cs = web_crawler.dedup.ContentSeen()
        fp1 = cs.compute_fingerprint(b"content A")
        fp2 = cs.compute_fingerprint(b"content B")
        cs.add(fp1)
        assert not cs.is_seen(fp2)


class TestURLSeenNormalization:
    """Test URLSeen normalization-based dedup."""

    def test_same_url_is_seen(self):
        us = web_crawler.dedup.URLSeen()
        us.add("http://example.com/page")
        assert us.is_seen("http://example.com/page")

    def test_different_case_scheme_is_seen(self):
        us = web_crawler.dedup.URLSeen()
        us.add("HTTP://example.com/page")
        assert us.is_seen("http://example.com/page")

    def test_different_case_host_is_seen(self):
        us = web_crawler.dedup.URLSeen()
        us.add("http://EXAMPLE.COM/page")
        assert us.is_seen("http://example.com/page")

    def test_fragment_ignored(self):
        us = web_crawler.dedup.URLSeen()
        us.add("http://example.com/page#section")
        assert us.is_seen("http://example.com/page")

    def test_different_url_not_seen(self):
        us = web_crawler.dedup.URLSeen()
        us.add("http://example.com/page1")
        assert not us.is_seen("http://example.com/page2")


class TestURLSeenCount:
    """Test URLSeen count property."""

    def test_count_initially_zero(self):
        us = web_crawler.dedup.URLSeen()
        assert us.count == 0

    def test_count_after_adds(self):
        us = web_crawler.dedup.URLSeen()
        us.add("http://example.com/page1")
        us.add("http://example.com/page2")
        assert us.count == 2

    def test_count_deduplicates(self):
        us = web_crawler.dedup.URLSeen()
        us.add("http://example.com/page")
        us.add("http://EXAMPLE.COM/page")  # Same after normalization
        assert us.count == 1
