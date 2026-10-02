"""Unit tests for the NewsFeedCache."""

from datetime import datetime, timezone, timedelta

from news_feed_system.models import FeedEntry
from news_feed_system.news_feed_cache import NewsFeedCache


def _make_entry(post_id: str, author_id: str = "author1", minutes_ago: int = 0) -> FeedEntry:
    """Helper to create a FeedEntry with a specific timestamp."""
    ts = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc) - timedelta(minutes=minutes_ago)
    return FeedEntry(post_id=post_id, author_id=author_id, timestamp=ts)


class TestNewsFeedCacheAppend:
    """Tests for append with eviction behavior."""

    def test_append_single_entry(self):
        cache = NewsFeedCache(max_entries=10)
        entry = _make_entry("p1")
        cache.append("user1", entry)
        assert cache.get_count("user1") == 1

    def test_append_multiple_entries(self):
        cache = NewsFeedCache(max_entries=10)
        for i in range(5):
            cache.append("user1", _make_entry(f"p{i}"))
        assert cache.get_count("user1") == 5

    def test_append_evicts_oldest_when_at_capacity(self):
        cache = NewsFeedCache(max_entries=3)
        # Add entries oldest to newest
        e1 = _make_entry("p1", minutes_ago=3)
        e2 = _make_entry("p2", minutes_ago=2)
        e3 = _make_entry("p3", minutes_ago=1)
        e4 = _make_entry("p4", minutes_ago=0)

        cache.append("user1", e1)
        cache.append("user1", e2)
        cache.append("user1", e3)
        # At capacity (3), now add one more
        cache.append("user1", e4)

        assert cache.get_count("user1") == 3
        entries = cache.get_entries("user1", page=1, page_size=10)
        post_ids = [e.post_id for e in entries]
        # Oldest (p1) should be evicted
        assert "p1" not in post_ids
        assert "p2" in post_ids
        assert "p3" in post_ids
        assert "p4" in post_ids

    def test_append_separate_users_independent(self):
        cache = NewsFeedCache(max_entries=5)
        cache.append("user1", _make_entry("p1"))
        cache.append("user2", _make_entry("p2"))
        assert cache.get_count("user1") == 1
        assert cache.get_count("user2") == 1


class TestNewsFeedCacheGetEntries:
    """Tests for paginated reverse-chronological retrieval."""

    def test_get_entries_reverse_chronological(self):
        cache = NewsFeedCache(max_entries=10)
        # Append oldest to newest
        e1 = _make_entry("p1", minutes_ago=3)
        e2 = _make_entry("p2", minutes_ago=2)
        e3 = _make_entry("p3", minutes_ago=1)

        cache.append("user1", e1)
        cache.append("user1", e2)
        cache.append("user1", e3)

        entries = cache.get_entries("user1", page=1, page_size=10)
        # Should be newest first
        assert entries[0].post_id == "p3"
        assert entries[1].post_id == "p2"
        assert entries[2].post_id == "p1"

    def test_get_entries_pagination_page_1(self):
        cache = NewsFeedCache(max_entries=10)
        for i in range(5):
            cache.append("user1", _make_entry(f"p{i}", minutes_ago=5 - i))

        entries = cache.get_entries("user1", page=1, page_size=2)
        assert len(entries) == 2
        # Newest first: p4, p3
        assert entries[0].post_id == "p4"
        assert entries[1].post_id == "p3"

    def test_get_entries_pagination_page_2(self):
        cache = NewsFeedCache(max_entries=10)
        for i in range(5):
            cache.append("user1", _make_entry(f"p{i}", minutes_ago=5 - i))

        entries = cache.get_entries("user1", page=2, page_size=2)
        assert len(entries) == 2
        # Next page: p2, p1
        assert entries[0].post_id == "p2"
        assert entries[1].post_id == "p1"

    def test_get_entries_pagination_last_page_partial(self):
        cache = NewsFeedCache(max_entries=10)
        for i in range(5):
            cache.append("user1", _make_entry(f"p{i}", minutes_ago=5 - i))

        entries = cache.get_entries("user1", page=3, page_size=2)
        assert len(entries) == 1
        assert entries[0].post_id == "p0"

    def test_get_entries_page_out_of_range(self):
        cache = NewsFeedCache(max_entries=10)
        cache.append("user1", _make_entry("p1"))
        entries = cache.get_entries("user1", page=5, page_size=10)
        assert entries == []

    def test_get_entries_empty_user(self):
        cache = NewsFeedCache(max_entries=10)
        entries = cache.get_entries("nonexistent", page=1, page_size=10)
        assert entries == []

    def test_get_entries_invalid_page(self):
        cache = NewsFeedCache(max_entries=10)
        cache.append("user1", _make_entry("p1"))
        assert cache.get_entries("user1", page=0, page_size=10) == []
        assert cache.get_entries("user1", page=-1, page_size=10) == []

    def test_get_entries_invalid_page_size(self):
        cache = NewsFeedCache(max_entries=10)
        cache.append("user1", _make_entry("p1"))
        assert cache.get_entries("user1", page=1, page_size=0) == []
        assert cache.get_entries("user1", page=1, page_size=-1) == []


class TestNewsFeedCacheRemoveEntry:
    """Tests for entry removal by post_id."""

    def test_remove_existing_entry(self):
        cache = NewsFeedCache(max_entries=10)
        cache.append("user1", _make_entry("p1"))
        cache.append("user1", _make_entry("p2"))
        cache.append("user1", _make_entry("p3"))

        result = cache.remove_entry("user1", "p2")
        assert result is True
        assert cache.get_count("user1") == 2
        entries = cache.get_entries("user1", page=1, page_size=10)
        post_ids = [e.post_id for e in entries]
        assert "p2" not in post_ids

    def test_remove_nonexistent_entry(self):
        cache = NewsFeedCache(max_entries=10)
        cache.append("user1", _make_entry("p1"))
        result = cache.remove_entry("user1", "p999")
        assert result is False
        assert cache.get_count("user1") == 1

    def test_remove_from_nonexistent_user(self):
        cache = NewsFeedCache(max_entries=10)
        result = cache.remove_entry("nonexistent", "p1")
        assert result is False


class TestNewsFeedCacheConfig:
    """Tests for configuration and edge cases."""

    def test_invalid_max_entries_raises(self):
        import pytest
        with pytest.raises(ValueError):
            NewsFeedCache(max_entries=0)
        with pytest.raises(ValueError):
            NewsFeedCache(max_entries=-1)

    def test_max_entries_property(self):
        cache = NewsFeedCache(max_entries=42)
        assert cache.max_entries == 42

    def test_clear_user_feed(self):
        cache = NewsFeedCache(max_entries=10)
        cache.append("user1", _make_entry("p1"))
        cache.append("user1", _make_entry("p2"))
        cache.clear("user1")
        assert cache.get_count("user1") == 0

    def test_get_count_empty(self):
        cache = NewsFeedCache(max_entries=10)
        assert cache.get_count("nonexistent") == 0
