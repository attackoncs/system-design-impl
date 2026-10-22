"""Unit tests for the ActionCache module."""

import news_feed_system.action_cache
import news_feed_system.models
import pytest


class TestActionCacheRecordAndQuery:
    """Tests for recording and querying actions."""

    def test_record_action_liked(self):
        cache = news_feed_system.action_cache.ActionCache(capacity=100)
        cache.record_action("user1", "post1", news_feed_system.models.ActionType.LIKED)
        state = cache.get_actions("user1", "post1")
        assert state.liked is True
        assert state.replied is False
        assert state.other_actions == []

    def test_record_action_replied(self):
        cache = news_feed_system.action_cache.ActionCache(capacity=100)
        cache.record_action("user1", "post1", news_feed_system.models.ActionType.REPLIED)
        state = cache.get_actions("user1", "post1")
        assert state.liked is False
        assert state.replied is True

    def test_record_multiple_actions(self):
        cache = news_feed_system.action_cache.ActionCache(capacity=100)
        cache.record_action("user1", "post1", news_feed_system.models.ActionType.LIKED)
        cache.record_action("user1", "post1", news_feed_system.models.ActionType.REPLIED)
        cache.record_action("user1", "post1", news_feed_system.models.ActionType.SHARED)
        state = cache.get_actions("user1", "post1")
        assert state.liked is True
        assert state.replied is True
        assert "shared" in state.other_actions

    def test_get_actions_nonexistent_returns_empty_state(self):
        cache = news_feed_system.action_cache.ActionCache(capacity=100)
        state = cache.get_actions("user1", "post1")
        assert state == news_feed_system.models.ActionState()

    def test_has_action_true(self):
        cache = news_feed_system.action_cache.ActionCache(capacity=100)
        cache.record_action("user1", "post1", news_feed_system.models.ActionType.LIKED)
        assert cache.has_action("user1", "post1", news_feed_system.models.ActionType.LIKED) is True

    def test_has_action_false(self):
        cache = news_feed_system.action_cache.ActionCache(capacity=100)
        cache.record_action("user1", "post1", news_feed_system.models.ActionType.LIKED)
        assert cache.has_action("user1", "post1", news_feed_system.models.ActionType.REPLIED) is False

    def test_has_action_nonexistent_entry(self):
        cache = news_feed_system.action_cache.ActionCache(capacity=100)
        assert cache.has_action("user1", "post1", news_feed_system.models.ActionType.LIKED) is False

    def test_separate_users_separate_actions(self):
        cache = news_feed_system.action_cache.ActionCache(capacity=100)
        cache.record_action("user1", "post1", news_feed_system.models.ActionType.LIKED)
        cache.record_action("user2", "post1", news_feed_system.models.ActionType.REPLIED)
        assert cache.has_action("user1", "post1", news_feed_system.models.ActionType.LIKED) is True
        assert cache.has_action("user1", "post1", news_feed_system.models.ActionType.REPLIED) is False
        assert cache.has_action("user2", "post1", news_feed_system.models.ActionType.REPLIED) is True
        assert cache.has_action("user2", "post1", news_feed_system.models.ActionType.LIKED) is False

    def test_separate_posts_separate_actions(self):
        cache = news_feed_system.action_cache.ActionCache(capacity=100)
        cache.record_action("user1", "post1", news_feed_system.models.ActionType.LIKED)
        cache.record_action("user1", "post2", news_feed_system.models.ActionType.REPLIED)
        assert cache.has_action("user1", "post1", news_feed_system.models.ActionType.LIKED) is True
        assert cache.has_action("user1", "post1", news_feed_system.models.ActionType.REPLIED) is False
        assert cache.has_action("user1", "post2", news_feed_system.models.ActionType.REPLIED) is True
        assert cache.has_action("user1", "post2", news_feed_system.models.ActionType.LIKED) is False


class TestActionCacheRemove:
    """Tests for removing actions."""

    def test_remove_action(self):
        cache = news_feed_system.action_cache.ActionCache(capacity=100)
        cache.record_action("user1", "post1", news_feed_system.models.ActionType.LIKED)
        cache.remove_action("user1", "post1", news_feed_system.models.ActionType.LIKED)
        assert cache.has_action("user1", "post1", news_feed_system.models.ActionType.LIKED) is False

    def test_remove_action_keeps_other_actions(self):
        cache = news_feed_system.action_cache.ActionCache(capacity=100)
        cache.record_action("user1", "post1", news_feed_system.models.ActionType.LIKED)
        cache.record_action("user1", "post1", news_feed_system.models.ActionType.REPLIED)
        cache.remove_action("user1", "post1", news_feed_system.models.ActionType.LIKED)
        assert cache.has_action("user1", "post1", news_feed_system.models.ActionType.LIKED) is False
        assert cache.has_action("user1", "post1", news_feed_system.models.ActionType.REPLIED) is True

    def test_remove_last_action_removes_entry(self):
        cache = news_feed_system.action_cache.ActionCache(capacity=100)
        cache.record_action("user1", "post1", news_feed_system.models.ActionType.LIKED)
        cache.remove_action("user1", "post1", news_feed_system.models.ActionType.LIKED)
        assert len(cache) == 0

    def test_remove_nonexistent_action_no_error(self):
        cache = news_feed_system.action_cache.ActionCache(capacity=100)
        # Should not raise
        cache.remove_action("user1", "post1", news_feed_system.models.ActionType.LIKED)

    def test_remove_action_from_nonexistent_entry(self):
        cache = news_feed_system.action_cache.ActionCache(capacity=100)
        cache.remove_action("user1", "post1", news_feed_system.models.ActionType.LIKED)
        assert len(cache) == 0


class TestActionCacheLRUEviction:
    """Tests for LRU eviction behavior."""

    def test_eviction_at_capacity(self):
        cache = news_feed_system.action_cache.ActionCache(capacity=3)
        cache.record_action("user1", "post1", news_feed_system.models.ActionType.LIKED)
        cache.record_action("user2", "post2", news_feed_system.models.ActionType.LIKED)
        cache.record_action("user3", "post3", news_feed_system.models.ActionType.LIKED)
        # This should evict (user1, post1)
        cache.record_action("user4", "post4", news_feed_system.models.ActionType.LIKED)
        assert len(cache) == 3
        assert cache.has_action("user1", "post1", news_feed_system.models.ActionType.LIKED) is False
        assert cache.has_action("user4", "post4", news_feed_system.models.ActionType.LIKED) is True

    def test_access_refreshes_lru_order(self):
        cache = news_feed_system.action_cache.ActionCache(capacity=3)
        cache.record_action("user1", "post1", news_feed_system.models.ActionType.LIKED)
        cache.record_action("user2", "post2", news_feed_system.models.ActionType.LIKED)
        cache.record_action("user3", "post3", news_feed_system.models.ActionType.LIKED)
        # Access user1/post1 to refresh it
        cache.get_actions("user1", "post1")
        # Now user2/post2 is LRU, should be evicted
        cache.record_action("user4", "post4", news_feed_system.models.ActionType.LIKED)
        assert cache.has_action("user1", "post1", news_feed_system.models.ActionType.LIKED) is True
        assert cache.has_action("user2", "post2", news_feed_system.models.ActionType.LIKED) is False

    def test_has_action_refreshes_lru_order(self):
        cache = news_feed_system.action_cache.ActionCache(capacity=3)
        cache.record_action("user1", "post1", news_feed_system.models.ActionType.LIKED)
        cache.record_action("user2", "post2", news_feed_system.models.ActionType.LIKED)
        cache.record_action("user3", "post3", news_feed_system.models.ActionType.LIKED)
        # has_action on user1/post1 refreshes it
        cache.has_action("user1", "post1", news_feed_system.models.ActionType.LIKED)
        # Now user2/post2 is LRU
        cache.record_action("user4", "post4", news_feed_system.models.ActionType.LIKED)
        assert cache.has_action("user1", "post1", news_feed_system.models.ActionType.LIKED) is True
        assert cache.has_action("user2", "post2", news_feed_system.models.ActionType.LIKED) is False

    def test_record_existing_entry_refreshes_lru(self):
        cache = news_feed_system.action_cache.ActionCache(capacity=3)
        cache.record_action("user1", "post1", news_feed_system.models.ActionType.LIKED)
        cache.record_action("user2", "post2", news_feed_system.models.ActionType.LIKED)
        cache.record_action("user3", "post3", news_feed_system.models.ActionType.LIKED)
        # Adding another action to user1/post1 refreshes it
        cache.record_action("user1", "post1", news_feed_system.models.ActionType.REPLIED)
        # Now user2/post2 is LRU
        cache.record_action("user4", "post4", news_feed_system.models.ActionType.LIKED)
        assert cache.has_action("user1", "post1", news_feed_system.models.ActionType.LIKED) is True
        assert cache.has_action("user2", "post2", news_feed_system.models.ActionType.LIKED) is False

    def test_capacity_one(self):
        cache = news_feed_system.action_cache.ActionCache(capacity=1)
        cache.record_action("user1", "post1", news_feed_system.models.ActionType.LIKED)
        cache.record_action("user2", "post2", news_feed_system.models.ActionType.LIKED)
        assert len(cache) == 1
        assert cache.has_action("user1", "post1", news_feed_system.models.ActionType.LIKED) is False
        assert cache.has_action("user2", "post2", news_feed_system.models.ActionType.LIKED) is True


class TestActionCacheConfiguration:
    """Tests for cache configuration."""

    def test_default_capacity(self):
        cache = news_feed_system.action_cache.ActionCache()
        assert cache.capacity == 10000

    def test_custom_capacity(self):
        cache = news_feed_system.action_cache.ActionCache(capacity=500)
        assert cache.capacity == 500

    def test_invalid_capacity_raises(self):
        with pytest.raises(ValueError):
            news_feed_system.action_cache.ActionCache(capacity=0)
        with pytest.raises(ValueError):
            news_feed_system.action_cache.ActionCache(capacity=-1)

    def test_len_empty(self):
        cache = news_feed_system.action_cache.ActionCache(capacity=100)
        assert len(cache) == 0

    def test_len_after_records(self):
        cache = news_feed_system.action_cache.ActionCache(capacity=100)
        cache.record_action("user1", "post1", news_feed_system.models.ActionType.LIKED)
        cache.record_action("user1", "post2", news_feed_system.models.ActionType.LIKED)
        assert len(cache) == 2
