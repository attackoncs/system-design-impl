"""URL Shortener orchestrator module.

Coordinates between hash strategies, storage backends, and click tracking to
provide the full shorten/redirect flow.
"""

from collections import OrderedDict, deque
import datetime
import threading
from typing import Optional

import url_shortener.exceptions
import url_shortener.storage
import url_shortener.strategies
from url_shortener import models
from url_shortener.urls import redirect_uri


# Raw click details are bounded per link; totals remain exact counters.
DEFAULT_CLICK_RETENTION_MAX_RECORDS = 1000
DEFAULT_CLICK_RETENTION_MAX_AGE = datetime.timedelta(days=30)


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
        click_retention_max_records: Optional[int] = DEFAULT_CLICK_RETENTION_MAX_RECORDS,
        click_retention_max_age: Optional[datetime.timedelta] = DEFAULT_CLICK_RETENTION_MAX_AGE,
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
            cache_capacity: Instance LRU size; 0 disables the cache.
            click_retention_max_records: Maximum raw ClickRecords kept per
                short code (oldest dropped first); 0 keeps no details,
                None removes the count bound.
            click_retention_max_age: Raw ClickRecords older than this are
                dropped; None removes the age bound.

        Click totals are exact counters and are never affected by retention.
        """
        if type(cache_capacity) is not int or cache_capacity < 0:
            raise ValueError("cache_capacity must be a nonnegative integer")
        self._cache_capacity = cache_capacity
        self._cache = OrderedDict()
        self._cache_lock = threading.Lock()
        self._strategy = strategy or url_shortener.strategies.HashCollisionStrategy()
        self._storage = storage or url_shortener.storage.InMemoryStorage()
        self._default_redirect_type = self._validate_redirect_type(default_redirect_type)
        self._domain = domain.rstrip("/")
        if click_retention_max_records is not None and (
            type(click_retention_max_records) is not int
            or click_retention_max_records < 0
        ):
            raise ValueError(
                "click_retention_max_records must be None or a nonnegative integer"
            )
        if click_retention_max_age is not None and (
            not isinstance(click_retention_max_age, datetime.timedelta)
            or click_retention_max_age <= datetime.timedelta(0)
        ):
            raise ValueError("click_retention_max_age must be None or a positive timedelta")
        self._click_max_records = click_retention_max_records
        self._click_max_age = click_retention_max_age
        self._click_lock = threading.Lock()
        # Exact O(1) total per link; independent of detail retention.
        self._click_counts: dict[str, int] = {}
        # Retained raw details per link, oldest first (bounded deque).
        self._clicks: dict[str, deque[models.ClickRecord]] = {}

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
        chosen_redirect = self._validate_redirect_type(
            self._default_redirect_type if redirect_type is None else redirect_type
        )

        def create_mapping():
            code = self._strategy.generate(long_url, self._storage)
            return models.URLMapping(
                short_code=code,
                long_url=long_url,
                redirect_type=chosen_redirect,
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

        self._record_click(short_code, client_id)

        return mapping.long_url, mapping.redirect_type

    def _record_click(self, short_code: str, client_id: Optional[str]) -> None:
        """Count a click exactly and retain its detail within the policy.

        The timestamp is taken under the lock so each per-link deque stays in
        append (time) order; age pruning then only pops expired records from
        the left, which is amortized O(1) per click and never scans.
        """
        with self._click_lock:
            now = datetime.datetime.now(datetime.timezone.utc)
            self._click_counts[short_code] = self._click_counts.get(short_code, 0) + 1
            if self._click_max_records == 0:
                return
            records = self._clicks.get(short_code)
            if records is None:
                # maxlen evicts the oldest record in O(1) when full.
                records = deque(maxlen=self._click_max_records)
                self._clicks[short_code] = records
            records.append(
                models.ClickRecord(short_code=short_code, timestamp=now, client_id=client_id)
            )
            self._prune_expired(records, now)

    def _prune_expired(self, records: deque, now: datetime.datetime) -> None:
        """Drop records older than the max age from the oldest end."""
        if self._click_max_age is None:
            return
        cutoff = now - self._click_max_age
        while records and records[0].timestamp < cutoff:
            records.popleft()

    def get_click_count(self, short_code: str) -> int:
        """Get the total number of clicks for a short code.

        The total is an exact counter and includes clicks whose details have
        already been dropped by the retention policy.

        Args:
            short_code: The short code to query.

        Returns:
            Total click count (0 if no clicks recorded).
        """
        with self._click_lock:
            return self._click_counts.get(short_code, 0)

    def get_click_records(self, short_code: str) -> list[models.ClickRecord]:
        """Get the retained click records for a short code.

        Only the retention window is returned: at most
        ``click_retention_max_records`` of the newest records, none older than
        ``click_retention_max_age``. Use ``get_click_count`` for the exact total.

        Args:
            short_code: The short code to query.

        Returns:
            List of ClickRecord objects, oldest first (empty if none retained).
        """
        with self._click_lock:
            records = self._clicks.get(short_code)
            if not records:
                return []
            self._prune_expired(records, datetime.datetime.now(datetime.timezone.utc))
            return list(records)

    def _validate_url(self, url: str) -> None:
        """Validate that a URL is well-formed.

        Args:
            url: The URL to validate.

        Raises:
            URLValidationError: If the URL is invalid.
        """
        redirect_uri(url)

    @staticmethod
    def _validate_redirect_type(value) -> models.RedirectType:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError("redirect type must be 301 or 302")
        return models.RedirectType(value)
