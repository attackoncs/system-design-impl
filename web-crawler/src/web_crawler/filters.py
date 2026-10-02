"""Configurable URL filter with multiple rule types."""

from __future__ import annotations

from typing import Callable, List, Optional, Set
from urllib.parse import urlparse


class URLFilter:
    """Configurable URL filter with multiple rule types.

    Applies rules in order:
    1. Scheme check (http/https only)
    2. URL length check
    3. Domain whitelist (if configured)
    4. Domain blacklist (if configured)
    5. Extension blacklist
    6. Custom filter functions
    """

    def __init__(
        self,
        extension_blacklist: Optional[List[str]] = None,
        domain_whitelist: Optional[List[str]] = None,
        domain_blacklist: Optional[List[str]] = None,
        max_url_length: int = 2048,
        custom_filters: Optional[List[Callable[[str], bool]]] = None,
    ) -> None:
        self._extension_blacklist: Set[str] = set(
            ext.lower() for ext in (extension_blacklist or [])
        )
        self._domain_whitelist: Optional[Set[str]] = (
            set(d.lower() for d in domain_whitelist) if domain_whitelist else None
        )
        self._domain_blacklist: Set[str] = set(
            d.lower() for d in (domain_blacklist or [])
        )
        self._max_url_length: int = max_url_length
        self._custom_filters: List[Callable[[str], bool]] = custom_filters or []

    def accept(self, url: str) -> bool:
        """Check if a URL passes all filter rules.

        Args:
            url: The URL to check.

        Returns:
            True if the URL is accepted, False if rejected.
        """
        parsed = urlparse(url)

        # 1. Scheme check (http/https only)
        if parsed.scheme.lower() not in ("http", "https"):
            return False

        # 2. URL length check
        if len(url) > self._max_url_length:
            return False

        # 3. Domain whitelist (must be in whitelist if configured)
        domain = parsed.netloc.lower()
        if self._domain_whitelist is not None:
            if domain not in self._domain_whitelist:
                return False

        # 4. Domain blacklist (URL must NOT be in blacklist)
        if domain in self._domain_blacklist:
            return False

        # 5. Extension blacklist (path must not end with blacklisted extension)
        path_lower = parsed.path.lower()
        for ext in self._extension_blacklist:
            if path_lower.endswith(ext):
                return False

        # 6. Custom filter functions (all must return True)
        for filter_fn in self._custom_filters:
            if not filter_fn(url):
                return False

        return True
