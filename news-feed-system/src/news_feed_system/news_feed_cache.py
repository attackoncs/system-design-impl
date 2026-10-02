"""News feed cache storing per-user feed entries with bounded capacity.

Provides a bounded in-memory cache of FeedEntry items per user. Entries are
stored in chronological order (oldest first) and retrieved in reverse
chronological order. When the configured maximum is exceeded, the oldest
entry is evicted.
"""

from collections import defaultdict
from typing import Dict, List, Optional

from news_feed_system.models import FeedEntry


class NewsFeedCache:
    """Bounded per-user news feed cache.

    Stores FeedEntry items (post_id, author_id mappings) per user with a
    configurable maximum number of entries. Supports append with eviction,
    paginated reverse-chronological retrieval, and entry removal.

    Args:
        max_entries: Maximum number of feed entries stored per user.
    """

    def __init__(self, max_entries: int = 500) -> None:
        if max_entries <= 0:
            raise ValueError(f"max_entries must be positive, got {max_entries}")
        self._max_entries = max_entries
        # user_id -> list of FeedEntry in chronological order (oldest first)
        self._feeds: Dict[str, List[FeedEntry]] = defaultdict(list)

    @property
    def max_entries(self) -> int:
        """The configured maximum entries per user."""
        return self._max_entries

    def append(self, user_id: str, entry: FeedEntry) -> None:
        """Append a feed entry for a user, evicting the oldest if at capacity.

        Args:
            user_id: The user whose feed to append to.
            entry: The FeedEntry to add.
        """
        feed = self._feeds[user_id]
        feed.append(entry)
        if len(feed) > self._max_entries:
            # Evict the oldest entry (front of the list)
            feed.pop(0)

    def get_entries(
        self, user_id: str, page: int = 1, page_size: int = 20
    ) -> List[FeedEntry]:
        """Retrieve feed entries in reverse chronological order with pagination.

        Args:
            user_id: The user whose feed to retrieve.
            page: The 1-based page number.
            page_size: Number of entries per page.

        Returns:
            A list of FeedEntry items for the requested page, ordered from
            newest to oldest. Returns an empty list if the user has no entries
            or the page is out of range.
        """
        if page < 1 or page_size < 1:
            return []

        feed = self._feeds.get(user_id)
        if not feed:
            return []

        # Reverse to get newest-first ordering
        reversed_feed = feed[::-1]

        # Calculate pagination slice
        start = (page - 1) * page_size
        end = start + page_size

        return reversed_feed[start:end]

    def remove_entry(self, user_id: str, post_id: str) -> bool:
        """Remove a specific entry from a user's feed by post_id.

        Args:
            user_id: The user whose feed to modify.
            post_id: The post_id of the entry to remove.

        Returns:
            True if an entry was removed, False if not found.
        """
        feed = self._feeds.get(user_id)
        if not feed:
            return False

        for i, entry in enumerate(feed):
            if entry.post_id == post_id:
                feed.pop(i)
                return True

        return False

    def get_count(self, user_id: str) -> int:
        """Get the number of entries in a user's feed.

        Args:
            user_id: The user whose feed count to retrieve.

        Returns:
            The number of entries currently stored for the user.
        """
        return len(self._feeds.get(user_id, []))

    def clear(self, user_id: str) -> None:
        """Clear all entries for a user.

        Args:
            user_id: The user whose feed to clear.
        """
        self._feeds.pop(user_id, None)
