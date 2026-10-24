"""Thread-safe, bounded per-user indexes containing feed IDs, not post bodies."""

import bisect
import threading

from news_feed_system.models import FeedEntry


class NewsFeedCache:
    """Keep each user's newest IDs ordered by ``(timestamp, post_id)``.

    Delivery retries are idempotent by post ID. Late fanout cannot displace a
    newer entry merely because it completed later. Capacity applies per user;
    production deployments must additionally shard or expire inactive users.
    """

    def __init__(self, max_entries: int = 500) -> None:
        if (
            isinstance(max_entries, bool)
            or not isinstance(max_entries, int)
            or max_entries <= 0
        ):
            raise ValueError("max_entries must be a positive integer")
        self._max_entries = max_entries
        self._feeds: dict[str, list[FeedEntry]] = {}
        self._lock = threading.RLock()

    @property
    def max_entries(self) -> int:
        return self._max_entries

    def append(self, user_id: str, entry: FeedEntry) -> None:
        """Insert once, retaining only the newest ``max_entries`` items."""
        with self._lock:
            feed = self._feeds.setdefault(user_id, [])
            if any(existing.post_id == entry.post_id for existing in feed):
                return
            index = bisect.bisect_right(
                [(item.timestamp, item.post_id) for item in feed],
                (entry.timestamp, entry.post_id),
            )
            feed.insert(index, entry)
            if len(feed) > self._max_entries:
                del feed[0]

    def get_entries(
        self, user_id: str, page: int = 1, page_size: int = 20
    ) -> list[FeedEntry]:
        """Return a detached page, newest first, using a deterministic ID tie-break."""
        if page < 1 or page_size < 1:
            return []
        with self._lock:
            feed = self._feeds.get(user_id, [])
            stop = len(feed) - (page - 1) * page_size
            if stop <= 0:
                return []
            return list(reversed(feed[max(0, stop - page_size) : stop]))

    def remove_entry(self, user_id: str, post_id: str) -> bool:
        with self._lock:
            feed = self._feeds.get(user_id, [])
            for index, entry in enumerate(feed):
                if entry.post_id == post_id:
                    del feed[index]
                    if not feed:
                        self._feeds.pop(user_id, None)
                    return True
            return False

    def get_count(self, user_id: str) -> int:
        with self._lock:
            return len(self._feeds.get(user_id, []))

    def clear(self, user_id: str) -> None:
        with self._lock:
            self._feeds.pop(user_id, None)
