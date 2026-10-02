"""Unit tests for FanoutService.

Covers:
- Non-celebrity fanout pushes to all followers (Req 3.1, 3.3, 4.1)
- Celebrity fanout does NOT push to followers (Req 3.1, 3.4)
- Mute list filtering excludes muted users (Req 12.1, 12.3)
- Selective sharing filtering includes only specified users (Req 12.4)
- Celebrity threshold boundary: exactly at threshold = celebrity (Req 3.1)
"""

from __future__ import annotations

import pytest

from news_feed_system.config import FanoutConfig
from news_feed_system.fanout_service import FanoutService
from news_feed_system.models import FanoutTask, Post
from news_feed_system.post_cache import PostCache
from news_feed_system.queue import MessageQueue
from news_feed_system.social_graph import InMemorySocialGraph
from news_feed_system.social_graph_cache import CachedSocialGraph


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_service(
    celebrity_threshold: int = 5,
    batch_size: int = 100,
) -> tuple[FanoutService, InMemorySocialGraph, PostCache, MessageQueue]:
    """Create a FanoutService with in-memory dependencies.

    Returns the service plus its underlying components for inspection.
    """
    graph = InMemorySocialGraph()
    cached_graph = CachedSocialGraph(graph, ttl_seconds=300.0)
    post_cache = PostCache(hot_capacity=100, normal_capacity=1000)
    queue = MessageQueue()
    config = FanoutConfig(
        celebrity_threshold=celebrity_threshold,
        batch_size=batch_size,
    )
    service = FanoutService(
        social_graph=cached_graph,
        post_cache=post_cache,
        queue=queue,
        config=config,
    )
    return service, graph, post_cache, queue


async def collect_all_tasks(queue: MessageQueue) -> list[FanoutTask]:
    """Drain all tasks currently in the queue and return them."""
    tasks: list[FanoutTask] = []
    while queue.size() > 0:
        tasks.append(await queue.dequeue())
    return tasks


def make_post(
    post_id: str = "p1",
    author_id: str = "author1",
    selective_sharing: list[str] | None = None,
) -> Post:
    return Post(
        post_id=post_id,
        author_id=author_id,
        content="Hello world",
        selective_sharing=selective_sharing,
    )


# ---------------------------------------------------------------------------
# Requirement 3.1 / 3.3: Non-celebrity fanout pushes to all followers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_celebrity_fanout_enqueues_tasks_for_all_followers() -> None:
    """Non-celebrity post fanout enqueues FanoutTasks covering all followers.

    Validates: Requirements 3.3, 4.1
    """
    service, graph, post_cache, queue = make_service(celebrity_threshold=5)

    # Add 3 followers — well below the celebrity threshold of 5
    for i in range(1, 4):
        await graph.add_follow(f"follower{i}", "author1")

    post = make_post()
    await service.fanout(post)

    tasks = await collect_all_tasks(queue)
    assert len(tasks) >= 1, "Expected at least one FanoutTask to be enqueued"

    # Collect all target user IDs across all batches
    all_targets: set[str] = set()
    for task in tasks:
        assert task.post_id == "p1"
        assert task.author_id == "author1"
        all_targets.update(task.target_user_ids)

    assert all_targets == {"follower1", "follower2", "follower3"}


@pytest.mark.asyncio
async def test_non_celebrity_fanout_does_not_store_in_post_cache() -> None:
    """Non-celebrity fanout should NOT store the post in PostCache.

    Validates: Requirements 3.3
    """
    service, graph, post_cache, queue = make_service(celebrity_threshold=5)

    await graph.add_follow("follower1", "author1")

    post = make_post()
    await service.fanout(post)

    # Post should not be in the post cache for non-celebrity
    assert not post_cache.contains("p1")


@pytest.mark.asyncio
async def test_non_celebrity_fanout_no_followers_enqueues_nothing() -> None:
    """Non-celebrity with zero followers enqueues no tasks.

    Validates: Requirements 3.3, 4.1
    """
    service, graph, post_cache, queue = make_service(celebrity_threshold=5)

    # No followers added
    post = make_post()
    await service.fanout(post)

    assert queue.size() == 0


# ---------------------------------------------------------------------------
# Requirement 3.1 / 3.4: Celebrity fanout does NOT push to followers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_celebrity_fanout_stores_post_in_cache_not_queue() -> None:
    """Celebrity post is stored in PostCache and NOT enqueued to followers.

    Validates: Requirements 3.1, 3.4
    """
    service, graph, post_cache, queue = make_service(celebrity_threshold=5)

    # Add 5 followers — exactly at the celebrity threshold
    for i in range(1, 6):
        await graph.add_follow(f"follower{i}", "author1")

    post = make_post()
    await service.fanout(post)

    # Post must be in the post cache
    assert post_cache.contains("p1"), "Celebrity post should be stored in PostCache"

    # No fanout tasks should be enqueued
    assert queue.size() == 0, "Celebrity fanout must not push tasks to the queue"


@pytest.mark.asyncio
async def test_celebrity_fanout_above_threshold_no_queue_tasks() -> None:
    """Celebrity with many followers still produces no queue tasks.

    Validates: Requirements 3.1, 3.4
    """
    service, graph, post_cache, queue = make_service(celebrity_threshold=5)

    # 10 followers — well above threshold
    for i in range(1, 11):
        await graph.add_follow(f"follower{i}", "author1")

    post = make_post()
    await service.fanout(post)

    assert post_cache.contains("p1")
    assert queue.size() == 0


# ---------------------------------------------------------------------------
# Requirement 3.1: Celebrity threshold boundary (exactly at threshold = celebrity)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_celebrity_threshold_boundary_exactly_at_threshold() -> None:
    """A user with follower_count == celebrity_threshold is classified as celebrity.

    Validates: Requirement 3.1
    """
    threshold = 10
    service, graph, post_cache, queue = make_service(celebrity_threshold=threshold)

    # Add exactly `threshold` followers
    for i in range(threshold):
        await graph.add_follow(f"follower{i}", "author1")

    is_celeb = await service.is_celebrity("author1")
    assert is_celeb is True, (
        f"User with exactly {threshold} followers should be a celebrity "
        f"(threshold={threshold})"
    )


@pytest.mark.asyncio
async def test_non_celebrity_threshold_one_below() -> None:
    """A user with follower_count == celebrity_threshold - 1 is NOT a celebrity.

    Validates: Requirement 3.1
    """
    threshold = 10
    service, graph, post_cache, queue = make_service(celebrity_threshold=threshold)

    # Add threshold - 1 followers
    for i in range(threshold - 1):
        await graph.add_follow(f"follower{i}", "author1")

    is_celeb = await service.is_celebrity("author1")
    assert is_celeb is False, (
        f"User with {threshold - 1} followers should NOT be a celebrity "
        f"(threshold={threshold})"
    )


@pytest.mark.asyncio
async def test_celebrity_threshold_boundary_fanout_behavior() -> None:
    """Fanout at exactly the threshold uses celebrity (pull) strategy.

    Validates: Requirements 3.1, 3.4
    """
    threshold = 3
    service, graph, post_cache, queue = make_service(celebrity_threshold=threshold)

    for i in range(threshold):
        await graph.add_follow(f"follower{i}", "author1")

    post = make_post()
    await service.fanout(post)

    # Celebrity path: post in cache, nothing in queue
    assert post_cache.contains("p1")
    assert queue.size() == 0


@pytest.mark.asyncio
async def test_non_celebrity_one_below_threshold_fanout_behavior() -> None:
    """Fanout one below the threshold uses non-celebrity (push) strategy.

    Validates: Requirements 3.1, 3.3
    """
    threshold = 3
    service, graph, post_cache, queue = make_service(celebrity_threshold=threshold)

    # threshold - 1 followers
    for i in range(threshold - 1):
        await graph.add_follow(f"follower{i}", "author1")

    post = make_post()
    await service.fanout(post)

    # Non-celebrity path: tasks in queue, post NOT in cache
    assert queue.size() > 0
    assert not post_cache.contains("p1")


# ---------------------------------------------------------------------------
# Requirement 12.1 / 12.3: Mute list filtering excludes muted users
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mute_list_excludes_muted_follower() -> None:
    """Followers who have muted the author are excluded from fanout.

    Validates: Requirements 12.1, 12.3
    """
    service, graph, post_cache, queue = make_service(celebrity_threshold=100)

    # Three followers; follower2 mutes the author
    for i in range(1, 4):
        await graph.add_follow(f"follower{i}", "author1")

    service.add_mute("follower2", "author1")

    post = make_post()
    await service.fanout(post)

    tasks = await collect_all_tasks(queue)
    all_targets: set[str] = set()
    for task in tasks:
        all_targets.update(task.target_user_ids)

    assert "follower2" not in all_targets, "Muted follower should be excluded"
    assert "follower1" in all_targets
    assert "follower3" in all_targets


@pytest.mark.asyncio
async def test_mute_list_all_followers_muted_enqueues_nothing() -> None:
    """When all followers have muted the author, no tasks are enqueued.

    Validates: Requirements 12.1, 12.3
    """
    service, graph, post_cache, queue = make_service(celebrity_threshold=100)

    for i in range(1, 4):
        await graph.add_follow(f"follower{i}", "author1")
        service.add_mute(f"follower{i}", "author1")

    post = make_post()
    await service.fanout(post)

    assert queue.size() == 0


@pytest.mark.asyncio
async def test_remove_mute_restores_follower_to_fanout() -> None:
    """Removing a mute restores the follower to the fanout target list.

    Validates: Requirements 12.1, 12.3
    """
    service, graph, post_cache, queue = make_service(celebrity_threshold=100)

    await graph.add_follow("follower1", "author1")
    service.add_mute("follower1", "author1")
    service.remove_mute("follower1", "author1")

    post = make_post()
    await service.fanout(post)

    tasks = await collect_all_tasks(queue)
    all_targets: set[str] = set()
    for task in tasks:
        all_targets.update(task.target_user_ids)

    assert "follower1" in all_targets


@pytest.mark.asyncio
async def test_mute_is_directional_author_muting_follower_has_no_effect() -> None:
    """Muting is follower-side: author muting a follower does not affect fanout.

    Validates: Requirements 12.1, 12.3
    """
    service, graph, post_cache, queue = make_service(celebrity_threshold=100)

    await graph.add_follow("follower1", "author1")

    # Author mutes follower1 — this should have no effect on fanout
    service.add_mute("author1", "follower1")

    post = make_post()
    await service.fanout(post)

    tasks = await collect_all_tasks(queue)
    all_targets: set[str] = set()
    for task in tasks:
        all_targets.update(task.target_user_ids)

    assert "follower1" in all_targets


# ---------------------------------------------------------------------------
# Requirement 12.4: Selective sharing filtering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_selective_sharing_includes_only_specified_followers() -> None:
    """Selective sharing restricts fanout to only the specified follower list.

    Validates: Requirement 12.4
    """
    service, graph, post_cache, queue = make_service(celebrity_threshold=100)

    for i in range(1, 6):
        await graph.add_follow(f"follower{i}", "author1")

    # Only follower1 and follower3 are in the selective sharing list
    post = make_post(selective_sharing=["follower1", "follower3"])
    await service.fanout(post)

    tasks = await collect_all_tasks(queue)
    all_targets: set[str] = set()
    for task in tasks:
        all_targets.update(task.target_user_ids)

    assert all_targets == {"follower1", "follower3"}, (
        "Only followers in the selective sharing list should receive the post"
    )


@pytest.mark.asyncio
async def test_selective_sharing_empty_list_enqueues_nothing() -> None:
    """An empty selective sharing list results in no fanout tasks.

    Validates: Requirement 12.4
    """
    service, graph, post_cache, queue = make_service(celebrity_threshold=100)

    for i in range(1, 4):
        await graph.add_follow(f"follower{i}", "author1")

    post = make_post(selective_sharing=[])
    await service.fanout(post)

    assert queue.size() == 0


@pytest.mark.asyncio
async def test_selective_sharing_none_means_public_to_all_followers() -> None:
    """selective_sharing=None means the post is public to all followers.

    Validates: Requirement 12.4
    """
    service, graph, post_cache, queue = make_service(celebrity_threshold=100)

    for i in range(1, 4):
        await graph.add_follow(f"follower{i}", "author1")

    post = make_post(selective_sharing=None)
    await service.fanout(post)

    tasks = await collect_all_tasks(queue)
    all_targets: set[str] = set()
    for task in tasks:
        all_targets.update(task.target_user_ids)

    assert all_targets == {"follower1", "follower2", "follower3"}


@pytest.mark.asyncio
async def test_selective_sharing_non_follower_in_list_is_ignored() -> None:
    """Users in selective_sharing who are not followers are not targeted.

    Validates: Requirement 12.4
    """
    service, graph, post_cache, queue = make_service(celebrity_threshold=100)

    await graph.add_follow("follower1", "author1")
    # "non_follower" is in the sharing list but does not follow the author

    post = make_post(selective_sharing=["follower1", "non_follower"])
    await service.fanout(post)

    tasks = await collect_all_tasks(queue)
    all_targets: set[str] = set()
    for task in tasks:
        all_targets.update(task.target_user_ids)

    assert "non_follower" not in all_targets
    assert "follower1" in all_targets


# ---------------------------------------------------------------------------
# Combined filtering: mute + selective sharing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mute_and_selective_sharing_combined() -> None:
    """Mute filter and selective sharing filter are both applied.

    A follower in the selective sharing list who has also muted the author
    should still be excluded.

    Validates: Requirements 12.3, 12.4
    """
    service, graph, post_cache, queue = make_service(celebrity_threshold=100)

    for i in range(1, 5):
        await graph.add_follow(f"follower{i}", "author1")

    # follower2 is in the sharing list but has muted the author
    service.add_mute("follower2", "author1")

    post = make_post(selective_sharing=["follower1", "follower2", "follower3"])
    await service.fanout(post)

    tasks = await collect_all_tasks(queue)
    all_targets: set[str] = set()
    for task in tasks:
        all_targets.update(task.target_user_ids)

    assert "follower2" not in all_targets, "Muted follower excluded even if in sharing list"
    assert "follower1" in all_targets
    assert "follower3" in all_targets
    assert "follower4" not in all_targets, "follower4 not in sharing list"


# ---------------------------------------------------------------------------
# Batching behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fanout_batches_followers_correctly() -> None:
    """Followers are split into batches of the configured batch_size.

    Validates: Requirement 4.4
    """
    batch_size = 3
    service, graph, post_cache, queue = make_service(
        celebrity_threshold=100, batch_size=batch_size
    )

    num_followers = 7
    for i in range(num_followers):
        await graph.add_follow(f"follower{i}", "author1")

    post = make_post()
    await service.fanout(post)

    tasks = await collect_all_tasks(queue)

    # With 7 followers and batch_size=3, expect ceil(7/3) = 3 tasks
    assert len(tasks) == 3

    # Verify all followers are covered exactly once
    all_targets: list[str] = []
    for task in tasks:
        all_targets.extend(task.target_user_ids)

    assert len(all_targets) == num_followers
    assert set(all_targets) == {f"follower{i}" for i in range(num_followers)}


# ---------------------------------------------------------------------------
# Mute list management API
# ---------------------------------------------------------------------------


def test_get_mute_list_returns_muted_users() -> None:
    """get_mute_list returns the set of users muted by a given user.

    Validates: Requirement 12.1
    """
    service, _, _, _ = make_service()

    service.add_mute("user1", "author_a")
    service.add_mute("user1", "author_b")

    muted = service.get_mute_list("user1")
    assert muted == {"author_a", "author_b"}


def test_get_mute_list_empty_for_new_user() -> None:
    """get_mute_list returns an empty set for a user with no mutes.

    Validates: Requirement 12.1
    """
    service, _, _, _ = make_service()

    muted = service.get_mute_list("new_user")
    assert muted == set()


def test_remove_mute_removes_only_specified_user() -> None:
    """remove_mute removes only the specified user from the mute list.

    Validates: Requirement 12.1
    """
    service, _, _, _ = make_service()

    service.add_mute("user1", "author_a")
    service.add_mute("user1", "author_b")
    service.remove_mute("user1", "author_a")

    muted = service.get_mute_list("user1")
    assert "author_a" not in muted
    assert "author_b" in muted


def test_remove_mute_nonexistent_is_noop() -> None:
    """remove_mute on a non-existent entry does not raise.

    Validates: Requirement 12.1
    """
    service, _, _, _ = make_service()

    # Should not raise
    service.remove_mute("user1", "nobody")
    assert service.get_mute_list("user1") == set()


# ---------------------------------------------------------------------------
# celebrity_threshold property
# ---------------------------------------------------------------------------


def test_celebrity_threshold_property_reflects_config() -> None:
    """celebrity_threshold property returns the configured value.

    Validates: Requirement 3.2
    """
    service, _, _, _ = make_service(celebrity_threshold=42)
    assert service.celebrity_threshold == 42
