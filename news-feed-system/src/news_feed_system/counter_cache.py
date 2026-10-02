"""Counter cache for tracking engagement metrics.

Stores atomic counters for likes, replies, followers, and following
keyed by (entity_id, counter_type). Counters are initialized to zero
on first access and cannot go below zero.
"""

from __future__ import annotations

from news_feed_system.models import CounterType


class CounterCache:
    """Cache for atomic counters (like count, reply count, follower/following count).

    Counters are stored by (entity_id, counter_type) key and initialized
    to zero on first access. Increment and decrement operations are atomic
    within a single-threaded asyncio context. Decrement operations are
    clamped to prevent counters from going below zero.
    """

    def __init__(self) -> None:
        self._counters: dict[tuple[str, CounterType], int] = {}

    def increment(self, entity_id: str, counter_type: CounterType) -> int:
        """Atomically increment a counter by 1.

        Args:
            entity_id: The entity (post or user) ID.
            counter_type: The type of counter to increment.

        Returns:
            The new counter value after incrementing.
        """
        key = (entity_id, counter_type)
        current = self._counters.get(key, 0)
        new_value = current + 1
        self._counters[key] = new_value
        return new_value

    def decrement(self, entity_id: str, counter_type: CounterType) -> int:
        """Atomically decrement a counter by 1, clamped at zero.

        Prevents the counter from going below zero. If the counter is
        already at zero, it remains at zero.

        Args:
            entity_id: The entity (post or user) ID.
            counter_type: The type of counter to decrement.

        Returns:
            The new counter value after decrementing (minimum 0).
        """
        key = (entity_id, counter_type)
        current = self._counters.get(key, 0)
        new_value = max(0, current - 1)
        self._counters[key] = new_value
        return new_value

    def get(self, entity_id: str, counter_type: CounterType) -> int:
        """Get the current value of a counter.

        Returns 0 if the counter has never been accessed (initialized
        to zero on first access).

        Args:
            entity_id: The entity (post or user) ID.
            counter_type: The type of counter to retrieve.

        Returns:
            The current counter value (0 if not previously set).
        """
        key = (entity_id, counter_type)
        return self._counters.get(key, 0)

    def get_batch(
        self, keys: list[tuple[str, CounterType]]
    ) -> dict[tuple[str, CounterType], int]:
        """Retrieve multiple counters in a single operation.

        Args:
            keys: List of (entity_id, counter_type) tuples to retrieve.

        Returns:
            Dictionary mapping each key to its current counter value.
            Missing counters are initialized to 0.
        """
        return {key: self._counters.get(key, 0) for key in keys}
