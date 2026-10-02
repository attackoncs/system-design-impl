"""Content parsing and link extraction for the web crawler.

Provides URL normalization, HTML content validation, and link extraction
from HTML pages using only Python standard library modules.
"""

from __future__ import annotations

from html.parser import HTMLParser
from typing import List, Optional, Tuple
from urllib.parse import urljoin, urlparse, urlunparse


class LinkExtractorParser(HTMLParser):
    """HTMLParser subclass that extracts href attributes from <a> tags."""

    def __init__(self) -> None:
        super().__init__()
        self.links: List[str] = []

    def handle_starttag(
        self, tag: str, attrs: List[Tuple[str, Optional[str]]]
    ) -> None:
        if tag == "a":
            for name, value in attrs:
                if name == "href" and value is not None:
                    self.links.append(value)


def normalize_url(url: str) -> str:
    """Normalize a URL by lowercasing scheme/host and removing fragments.

    The path and query string are kept unchanged.

    Args:
        url: The URL to normalize.

    Returns:
        Normalized URL string.
    """
    parsed = urlparse(url)
    normalized = parsed._replace(
        scheme=parsed.scheme.lower(),
        netloc=parsed.netloc.lower(),
        fragment="",
    )
    return urlunparse(normalized)


class ContentParser:
    """Validates that content is HTML based on Content-Type or content sniffing."""

    def is_html(self, content_type: str, body: bytes) -> bool:
        """Check if content is HTML based on Content-Type header or content sniffing.

        First checks if "text/html" appears in the content_type string.
        If content_type is empty or doesn't indicate HTML, falls back to
        sniffing the first 512 bytes of the body for <html or <!doctype markers.

        Args:
            content_type: The Content-Type header value.
            body: The response body bytes.

        Returns:
            True if content appears to be HTML.
        """
        if content_type:
            return "text/html" in content_type.lower()
        # Content sniffing fallback: check for HTML markers in first 512 bytes
        snippet = body[:512].lower()
        return b"<html" in snippet or b"<!doctype" in snippet


class LinkExtractor:
    """Extracts and normalizes links from HTML content."""

    def extract(self, html: str, base_url: str) -> List[str]:
        """Extract all links from HTML, resolving relative URLs.

        Uses LinkExtractorParser to find all href attributes in <a> tags,
        resolves relative URLs using urljoin with the base_url, and
        normalizes all results.

        Args:
            html: The HTML content string.
            base_url: The base URL for resolving relative links.

        Returns:
            List of absolute, normalized URL strings.
        """
        parser = LinkExtractorParser()
        parser.feed(html)

        results: List[str] = []
        for href in parser.links:
            # Resolve relative URLs
            absolute = urljoin(base_url, href)
            # Normalize
            normalized = normalize_url(absolute)
            if normalized:
                results.append(normalized)
        return results
