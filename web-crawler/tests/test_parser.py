"""Unit tests for parser module (normalize_url, ContentParser, LinkExtractor)."""

import pytest

from web_crawler.parser import ContentParser, LinkExtractor, normalize_url


class TestNormalizeUrl:
    """Test normalize_url function."""

    def test_lowercase_scheme(self):
        assert normalize_url("HTTP://example.com/page") == "http://example.com/page"

    def test_lowercase_host(self):
        assert normalize_url("http://EXAMPLE.COM/page") == "http://example.com/page"

    def test_remove_fragment(self):
        assert normalize_url("http://example.com/page#section") == "http://example.com/page"

    def test_keep_path(self):
        result = normalize_url("http://example.com/Path/To/Page")
        assert "/Path/To/Page" in result

    def test_keep_query(self):
        result = normalize_url("http://example.com/page?q=test")
        assert "?q=test" in result

    def test_combined_normalization(self):
        result = normalize_url("HTTPS://Example.COM/Path?q=1#frag")
        assert result == "https://example.com/Path?q=1"


class TestContentParserIsHtml:
    """Test ContentParser.is_html."""

    def test_text_html_content_type(self):
        parser = ContentParser()
        assert parser.is_html("text/html; charset=utf-8", b"<html></html>")

    def test_non_html_content_type(self):
        parser = ContentParser()
        assert not parser.is_html("application/json", b'{"key": "value"}')

    def test_content_sniffing_html_tag(self):
        parser = ContentParser()
        assert parser.is_html("", b"<html><body>Hello</body></html>")

    def test_content_sniffing_doctype(self):
        parser = ContentParser()
        assert parser.is_html("", b"<!DOCTYPE html><html></html>")

    def test_content_sniffing_no_html(self):
        parser = ContentParser()
        assert not parser.is_html("", b"Just plain text content")


class TestLinkExtractor:
    """Test LinkExtractor.extract with relative and absolute URLs."""

    def test_absolute_url(self):
        extractor = LinkExtractor()
        html = '<a href="http://other.com/page">Link</a>'
        links = extractor.extract(html, "http://example.com/")
        assert "http://other.com/page" in links

    def test_relative_url(self):
        extractor = LinkExtractor()
        html = '<a href="/about">About</a>'
        links = extractor.extract(html, "http://example.com/page")
        assert "http://example.com/about" in links

    def test_relative_path_url(self):
        extractor = LinkExtractor()
        html = '<a href="sub/page">Sub</a>'
        links = extractor.extract(html, "http://example.com/dir/")
        assert "http://example.com/dir/sub/page" in links

    def test_multiple_links(self):
        extractor = LinkExtractor()
        html = '<a href="/a">A</a><a href="/b">B</a><a href="/c">C</a>'
        links = extractor.extract(html, "http://example.com/")
        assert len(links) == 3

    def test_normalizes_extracted_urls(self):
        extractor = LinkExtractor()
        html = '<a href="http://EXAMPLE.COM/page#frag">Link</a>'
        links = extractor.extract(html, "http://example.com/")
        assert "http://example.com/page" in links

    def test_no_links(self):
        extractor = LinkExtractor()
        html = "<p>No links here</p>"
        links = extractor.extract(html, "http://example.com/")
        assert links == []
