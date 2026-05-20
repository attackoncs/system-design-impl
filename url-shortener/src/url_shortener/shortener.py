"""URL Shortener orchestrator module.

Coordinates between hash strategies, storage backends, and click tracking
to provide the full shorten/redirect flow.
"""

from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

from url_shortener.exceptions import ShortCodeNotFoundError, URLValidationError
from url_shortener.models import ClickRecord, RedirectType, URLMapping
from url_shortener.storage import InMemoryStorage, StorageBackend
from url_shortener.strategies import HashCollisionStrategy, HashStrategy


class URLShortener:
    """Main orchestrator for URL shortening and redirecting.

    Coordinates between hash strategies, storage backends, and
    click tracking to provide the full shorten/redirect flow.
    """

    def __init__(
        self,
        strategy: Optional[HashStrategy] = None,
        storage: Optional[StorageBackend] = None,
        default_redirect_type: RedirectType = RedirectType.TEMPORARY,
        domain: str = "http://short.url",
    ) -> None:
        """Initialize the URL shortener.

        Args:
            strategy: Hash strategy for code generation. Defaults to HashCollisionStrategy.
            storage: Storage backend for persistence. Defaults to InMemoryStorage.
            default_redirect_type: Default redirect type for new mappings. Defaults to 302.
            domain: Custom domain for constructing short URLs.
        """
        self._strategy = strategy or HashCollisionStrategy()
        self._storage = storage or InMemoryStorage()
        self._default_redirect_type = default_redirect_type
        self._domain = domain.rstrip("/")
        self._clicks: dict[str, list[ClickRecord]] = {}

    def shorten(
        self,
        long_url: str,
        redirect_type: Optional[RedirectType] = None,
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

        # Check if already shortened (idempotence)
        existing = self._storage.get_by_long_url(long_url)
        if existing is not None:
            return f"{self._domain}/{existing.short_code}"

        # Generate short code via strategy
        short_code = self._strategy.generate(long_url, self._storage)

        # Store the mapping
        mapping = URLMapping(
            short_code=short_code,
            long_url=long_url,
            redirect_type=redirect_type or self._default_redirect_type,
        )
        self._storage.save(mapping)

        return f"{self._domain}/{short_code}"

    def resolve(
        self,
        short_code: str,
        client_id: Optional[str] = None,
    ) -> tuple[str, RedirectType]:
        """Resolve a short code to the original long URL.

        Args:
            short_code: The 7-character short code to resolve.
            client_id: Optional client identifier for analytics.

        Returns:
            Tuple of (long_url, redirect_type).

        Raises:
            ShortCodeNotFoundError: If the short code doesn't exist.
        """
        mapping = self._storage.get_by_short_code(short_code)
        if mapping is None:
            raise ShortCodeNotFoundError(
                f"Short code '{short_code}' not found"
            )

        # Record click for analytics
        click = ClickRecord(
            short_code=short_code,
            timestamp=datetime.now(timezone.utc),
            client_id=client_id,
        )
        self._clicks.setdefault(short_code, []).append(click)

        return mapping.long_url, mapping.redirect_type

    def get_click_count(self, short_code: str) -> int:
        """Get the total number of clicks for a short code.

        Args:
            short_code: The short code to query.

        Returns:
            Total click count (0 if no clicks recorded).
        """
        return len(self._clicks.get(short_code, []))

    def get_click_records(self, short_code: str) -> list[ClickRecord]:
        """Get all click records for a short code.

        Args:
            short_code: The short code to query.

        Returns:
            List of ClickRecord objects (empty if no clicks).
        """
        return list(self._clicks.get(short_code, []))

    def _validate_url(self, url: str) -> None:
        """Validate that a URL is well-formed.

        Args:
            url: The URL to validate.

        Raises:
            URLValidationError: If the URL is invalid.
        """
        if not url or not url.strip():
            raise URLValidationError("URL cannot be empty")

        parsed = urlparse(url)
        if not parsed.scheme:
            raise URLValidationError(
                f"URL missing scheme (http/https): '{url}'"
            )
        if not parsed.netloc:
            raise URLValidationError(
                f"URL missing network location: '{url}'"
            )
