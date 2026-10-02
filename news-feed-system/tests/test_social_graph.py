"""Tests for the social graph abstraction."""

import pytest

from news_feed_system.social_graph import InMemorySocialGraph, SocialGraph


class TestSocialGraphABC:
    """Tests for the SocialGraph abstract base class."""

    def test_cannot_instantiate_abc(self):
        """SocialGraph ABC cannot be instantiated directly."""
        with pytest.raises(TypeError):
            SocialGraph()  # type: ignore

    def test_inmemory_is_subclass(self):
        """InMemorySocialGraph is a proper subclass of SocialGraph."""
        assert issubclass(InMemorySocialGraph, SocialGraph)

    def test_inmemory_is_instance(self):
        """InMemorySocialGraph instances satisfy the SocialGraph interface."""
        graph = InMemorySocialGraph()
        assert isinstance(graph, SocialGraph)


class TestInMemorySocialGraphAddFollow:
    """Tests for add_follow functionality."""

    async def test_add_follow_basic(self):
        """Adding a follow creates the relationship."""
        graph = InMemorySocialGraph()
        await graph.add_follow("alice", "bob")

        assert await graph.is_following("alice", "bob")

    async def test_add_follow_bidirectional_consistency(self):
        """add_follow updates both followers and following."""
        graph = InMemorySocialGraph()
        await graph.add_follow("alice", "bob")

        followers = await graph.get_followers("bob")
        following = await graph.get_following("alice")

        assert "alice" in followers
        assert "bob" in following

    async def test_add_follow_idempotent(self):
        """Adding the same follow twice does not duplicate."""
        graph = InMemorySocialGraph()
        await graph.add_follow("alice", "bob")
        await graph.add_follow("alice", "bob")

        followers = await graph.get_followers("bob")
        assert followers.count("alice") == 1
        assert await graph.get_follower_count("bob") == 1

    async def test_add_multiple_followers(self):
        """A user can have multiple followers."""
        graph = InMemorySocialGraph()
        await graph.add_follow("alice", "charlie")
        await graph.add_follow("bob", "charlie")

        followers = await graph.get_followers("charlie")
        assert set(followers) == {"alice", "bob"}
        assert await graph.get_follower_count("charlie") == 2

    async def test_add_follow_multiple_following(self):
        """A user can follow multiple users."""
        graph = InMemorySocialGraph()
        await graph.add_follow("alice", "bob")
        await graph.add_follow("alice", "charlie")

        following = await graph.get_following("alice")
        assert set(following) == {"bob", "charlie"}


class TestInMemorySocialGraphRemoveFollow:
    """Tests for remove_follow functionality."""

    async def test_remove_follow_basic(self):
        """Removing a follow deletes the relationship."""
        graph = InMemorySocialGraph()
        await graph.add_follow("alice", "bob")
        await graph.remove_follow("alice", "bob")

        assert not await graph.is_following("alice", "bob")

    async def test_remove_follow_bidirectional_consistency(self):
        """remove_follow updates both followers and following."""
        graph = InMemorySocialGraph()
        await graph.add_follow("alice", "bob")
        await graph.remove_follow("alice", "bob")

        followers = await graph.get_followers("bob")
        following = await graph.get_following("alice")

        assert "alice" not in followers
        assert "bob" not in following

    async def test_remove_nonexistent_follow(self):
        """Removing a non-existent follow does not raise."""
        graph = InMemorySocialGraph()
        # Should not raise
        await graph.remove_follow("alice", "bob")

    async def test_remove_preserves_other_relationships(self):
        """Removing one follow does not affect others."""
        graph = InMemorySocialGraph()
        await graph.add_follow("alice", "bob")
        await graph.add_follow("alice", "charlie")
        await graph.remove_follow("alice", "bob")

        assert not await graph.is_following("alice", "bob")
        assert await graph.is_following("alice", "charlie")


class TestInMemorySocialGraphQueries:
    """Tests for query methods."""

    async def test_get_followers_empty(self):
        """get_followers returns empty list for user with no followers."""
        graph = InMemorySocialGraph()
        followers = await graph.get_followers("alice")
        assert followers == []

    async def test_get_following_empty(self):
        """get_following returns empty list for user following nobody."""
        graph = InMemorySocialGraph()
        following = await graph.get_following("alice")
        assert following == []

    async def test_get_follower_count_zero(self):
        """get_follower_count returns 0 for user with no followers."""
        graph = InMemorySocialGraph()
        count = await graph.get_follower_count("alice")
        assert count == 0

    async def test_is_following_false(self):
        """is_following returns False when no relationship exists."""
        graph = InMemorySocialGraph()
        assert not await graph.is_following("alice", "bob")

    async def test_get_followers_returns_list(self):
        """get_followers returns a list type."""
        graph = InMemorySocialGraph()
        await graph.add_follow("alice", "bob")
        result = await graph.get_followers("bob")
        assert isinstance(result, list)

    async def test_get_following_returns_list(self):
        """get_following returns a list type."""
        graph = InMemorySocialGraph()
        await graph.add_follow("alice", "bob")
        result = await graph.get_following("alice")
        assert isinstance(result, list)
