"""Storage interfaces and in-memory URL mapping implementations."""

import abc
import threading
from typing import Optional

import url_shortener.models


class StorageBackend(abc.ABC):
    """Abstract interface for URL mapping persistence.

    Any storage backend can be used with any hash strategy, keeping the storage
    layer stateless with respect to code generation.
    """

    def get_or_create(self, long_url, factory):
        """Return a mapping, creating it if absent.

        Concurrent backends must override this with one atomic transaction;
        this fallback preserves compatibility with sequential custom backends.
        """
        existing = self.get_by_long_url(long_url)
        if existing is not None:
            return existing
        mapping = factory()
        self.save(mapping)
        return mapping

    @abc.abstractmethod
    def save(self, mapping: url_shortener.models.URLMapping) -> None:
        """Save a URL mapping to storage.

        Args:
            mapping: The URLMapping to persist.
        """

    @abc.abstractmethod
    def get_by_short_code(
        self, short_code: str
    ) -> Optional[url_shortener.models.URLMapping]:
        """Retrieve a URL mapping by its short code.

        Args:
            short_code: The 7-character short code.

        Returns:
            The URLMapping if found, None otherwise.
        """

    @abc.abstractmethod
    def get_by_long_url(
        self, long_url: str
    ) -> Optional[url_shortener.models.URLMapping]:
        """Retrieve a URL mapping by the original long URL.

        Args:
            long_url: The original long URL.

        Returns:
            The URLMapping if found, None otherwise.
        """

    @abc.abstractmethod
    def exists(self, short_code: str) -> bool:
        """Check if a short code exists in storage.

        Args:
            short_code: The 7-character short code.

        Returns:
            True if the short code exists, False otherwise.
        """


class InMemoryStorage(StorageBackend):
    """In-memory storage backend using Python dictionaries.

    Maintains bidirectional lookup for O(1) access in both directions.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_short_code: dict[str, url_shortener.models.URLMapping] = {}
        self._by_long_url: dict[str, url_shortener.models.URLMapping] = {}

    def get_or_create(self, long_url, factory):
        """Serialize read/generate/save across clients sharing this backend."""
        with self._lock:
            return super().get_or_create(long_url, factory)

    def save(self, mapping: url_shortener.models.URLMapping) -> None:
        """Save mapping with bidirectional indexing."""
        with self._lock:
            if self._by_short_code.get(mapping.short_code) not in (
                None,
                mapping,
            ) or self._by_long_url.get(mapping.long_url) not in (None, mapping):
                raise ValueError("mapping conflicts with an existing immutable mapping")
            self._by_short_code[mapping.short_code] = mapping
            self._by_long_url[mapping.long_url] = mapping

    def get_by_short_code(
        self, short_code: str
    ) -> Optional[url_shortener.models.URLMapping]:
        """O(1) lookup by short code."""
        with self._lock:
            return self._by_short_code.get(short_code)

    def get_by_long_url(
        self, long_url: str
    ) -> Optional[url_shortener.models.URLMapping]:
        """O(1) lookup by long URL."""
        with self._lock:
            return self._by_long_url.get(long_url)

    def exists(self, short_code: str) -> bool:
        """O(1) existence check."""
        with self._lock:
            return short_code in self._by_short_code
