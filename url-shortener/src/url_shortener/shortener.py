"""URL Shortener orchestrator module.

Coordinates between hash strategies, storage backends, and click tracking to
provide the full shorten/redirect flow.
"""

from collections import OrderedDict
import datetime
import threading
import urllib.parse
from typing import Optional

import url_shortener.exceptions
import url_shortener.storage
import url_shortener.strategies
from url_shortener import models


class URLShortener:
    """Main orchestrator for URL shortening and redirecting.

    Coordinates between hash strategies, storage backends, and click tracking
    to provide the full shorten/redirect flow.
    """

    def __init__(
        self,
        strategy: Optional[url_shortener.strategies.HashStrategy] = None,
        storage: Optional[url_shortener.storage.StorageBackend] = None,
        default_redirect_type: models.RedirectType = models.RedirectType.TEMPORARY,
        domain: str = "http://short.url",
        cache_capacity: int = 1024,
    ) -> None:
        """Initialize the URL shortener.

        Args:
            strategy: Hash strategy for code generation. Defaults to
                HashCollisionStrategy.
            storage: Storage backend for persistence. Defaults to
                InMemoryStorage.
            default_redirect_type: Default redirect type for new mappings.
                Defaults to 302.
            domain: Custom domain for constructing short URLs.
        """
        if type(cache_capacity) is not int or cache_capacity < 0:
            raise ValueError("cache_capacity must be a nonnegative integer")
        self._cache_capacity = cache_capacity
        self._cache = OrderedDict()
        self._cache_lock = threading.Lock()
        self._strategy = strategy or url_shortener.strategies.HashCollisionStrategy()
        self._storage = storage or url_shortener.storage.InMemoryStorage()
        self._default_redirect_type = default_redirect_type
        self._domain = domain.rstrip("/")
        self._click_lock = threading.Lock()
        self._clicks: dict[str, list[models.ClickRecord]] = {}

    def shorten(
        self,
        long_url: str,
        redirect_type: Optional[models.RedirectType] = None,
    ) -> str:
        """Shorten a long URL to a short code.

        Args:
            long_url: The URL to shorten.
            redirect_type: Override redirect type for this mapping.

        Returns:
            The full short URL (domain + "/" + short_code).

        Raises:
            URLValidationError: If the URL is invalid.
        """
        self._validate_url(long_url)

        def create_mapping():
            code = self._strategy.generate(long_url, self._storage)
            return models.URLMapping(
                short_code=code,
                long_url=long_url,
                redirect_type=redirect_type or self._default_redirect_type,
            )

        mapping = self._storage.get_or_create(long_url, create_mapping)
        return f"{self._domain}/{mapping.short_code}"

    def resolve(
        self,
        short_code: str,
        client_id: Optional[str] = None,
    ) -> tuple[str, models.RedirectType]:
        """Resolve a short code to the original long URL.

        Args:
            short_code: The 7-character short code to resolve.
            client_id: Optional client identifier for analytics.

        Returns:
            Tuple of (long_url, redirect_type).

        Raises:
            ShortCodeNotFoundError: If the short code doesn't exist.
        """
        with self._cache_lock:
            mapping = self._cache.get(short_code)
            if mapping is not None:
                self._cache.move_to_end(short_code)
        if mapping is None:
            mapping = self._storage.get_by_short_code(short_code)
            if mapping is not None and self._cache_capacity:
                with self._cache_lock:
                    self._cache[short_code] = mapping
                    self._cache.move_to_end(short_code)
                    while len(self._cache) > self._cache_capacity:
                        self._cache.popitem(last=False)
        if mapping is None:
            raise url_shortener.exceptions.ShortCodeNotFoundError(
                f"Short code '{short_code}' not found"
            )

        # Record click for analytics
        click = models.ClickRecord(
            short_code=short_code,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
            client_id=client_id,
        )
        with self._click_lock:
            self._clicks.setdefault(short_code, []).append(click)

        return mapping.long_url, mapping.redirect_type

    def get_click_count(self, short_code: str) -> int:
        """Get the total number of clicks for a short code.

        Args:
            short_code: The short code to query.

        Returns:
            Total click count (0 if no clicks recorded).
        """
        with self._click_lock:
            return len(self._clicks.get(short_code, []))

    def get_click_records(self, short_code: str) -> list[models.ClickRecord]:
        """Get all click records for a short code.

        Args:
            short_code: The short code to query.

        Returns:
            List of ClickRecord objects (empty if no clicks).
        """
        with self._click_lock:
            return list(self._clicks.get(short_code, []))

    def _validate_url(self, url: str) -> None:
        """Validate that a URL is well-formed.

        Args:
            url: The URL to validate.

        Raises:
            URLValidationError: If the URL is invalid.
        """
        if (
            not isinstance(url, str)
            or not url
            or any(ord(c) < 32 or ord(c) == 127 for c in url)
        ):
            raise url_shortener.exceptions.URLValidationError("URL cannot be empty")

        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise url_shortener.exceptions.URLValidationError(
                f"URL missing scheme (http/https): '{url}'"
            )
        if not parsed.netloc:
            raise url_shortener.exceptions.URLValidationError(
                f"URL missing network location: '{url}'"
            )
