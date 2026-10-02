"""Content and URL deduplication for the web crawler."""

from __future__ import annotations

import hashlib

from web_crawler.parser import normalize_url


class ContentSeen:
    """Content deduplication using SHA-256 fingerprints.

    Maintains an in-memory set of content hashes to detect
    duplicate page content.
    """

    def __init__(self) -> None:
        self._fingerprints: set[str] = set()

    def compute_fingerprint(self, content: bytes) -> str:
        """Compute SHA-256 fingerprint of content.

        Args:
            content: The page body bytes.

        Returns:
            Hex digest string of the SHA-256 hash.
        """
        return hashlib.sha256(content).hexdigest()

    def is_seen(self, fingerprint: str) -> bool:
        """Check if a content fingerprint has been seen before.

        Args:
            fingerprint: SHA-256 hex digest.

        Returns:
            True if this content has been seen before.
        """
        return fingerprint in self._fingerprints

    def add(self, fingerprint: str) -> None:
        """Mark a content fingerprint as seen.

        Args:
            fingerprint: SHA-256 hex digest to record.
        """
        self._fingerprints.add(fingerprint)


class URLSeen:
    """URL deduplication using normalized URL set.

    Normalizes URLs (lowercase scheme/host, remove fragments)
    before comparison to avoid visiting equivalent URLs.
    """

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def is_seen(self, url: str) -> bool:
        """Check if a URL has been seen (after normalization).

        Args:
            url: The URL to check.

        Returns:
            True if this URL (or a normalized equivalent) has been seen.
        """
        return normalize_url(url) in self._seen

    def add(self, url: str) -> None:
        """Mark a URL as seen (stores normalized form).

        Args:
            url: The URL to record.
        """
        self._seen.add(normalize_url(url))

    @property
    def count(self) -> int:
        """Number of unique URLs seen."""
        return len(self._seen)
