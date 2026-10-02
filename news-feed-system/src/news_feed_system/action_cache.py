"""Action cache for storing per-user, per-post interaction states.

Tracks user actions (liked, replied, shared, other) on posts with
configurable capacity and LRU eviction.
"""

from collections import OrderedDict

from news_feed_system.models import ActionState, ActionType


class ActionCache:
    """Cache for user action states on posts.

    Stores per-user, per-post action sets with LRU eviction when
    capacity is exceeded. The cache key is a (user_id, post_id) tuple.

    Args:
        capacity: Maximum number of (user_id, post_id) entries to store.
    """

    def __init__(self, capacity: int = 10000) -> None:
        if capacity <= 0:
            raise ValueError(f"capacity must be positive, got {capacity}")
        self._capacity = capacity
        # Key: (user_id, post_id) -> set of ActionType
        self._store: OrderedDict[tuple[str, str], set[ActionType]] = OrderedDict()

    @property
    def capacity(self) -> int:
        """Return the configured capacity."""
        return self._capacity

    def __len__(self) -> int:
        """Return the number of entries in the cache."""
        return len(self._store)

    def record_action(
        self, user_id: str, post_id: str, action_type: ActionType
    ) -> None:
        """Record a user action on a post.

        If the (user_id, post_id) entry already exists, the action is added
        to the existing set and the entry is moved to most-recently-used.
        If the entry is new and the cache is at capacity, the least recently
        used entry is evicted.

        Args:
            user_id: The user performing the action.
            post_id: The post being acted upon.
            action_type: The type of action being recorded.
        """
        key = (user_id, post_id)
        if key in self._store:
            self._store.move_to_end(key)
            self._store[key].add(action_type)
        else:
            self._evict_if_needed()
            self._store[key] = {action_type}

    def remove_action(
        self, user_id: str, post_id: str, action_type: ActionType
    ) -> None:
        """Remove a user action from a post (e.g., unlike).

        If the action set becomes empty after removal, the entry is
        removed from the cache entirely.

        Args:
            user_id: The user whose action is being removed.
            post_id: The post the action is being removed from.
            action_type: The type of action to remove.
        """
        key = (user_id, post_id)
        if key not in self._store:
            return
        self._store[key].discard(action_type)
        if not self._store[key]:
            del self._store[key]
        else:
            self._store.move_to_end(key)

    def get_actions(self, user_id: str, post_id: str) -> ActionState:
        """Get the action state for a user on a specific post.

        Accessing an entry marks it as most-recently-used.

        Args:
            user_id: The user to query.
            post_id: The post to query.

        Returns:
            An ActionState reflecting the user's interactions with the post.
        """
        key = (user_id, post_id)
        if key not in self._store:
            return ActionState()
        self._store.move_to_end(key)
        actions = self._store[key]
        return ActionState(
            liked=ActionType.LIKED in actions,
            replied=ActionType.REPLIED in actions,
            other_actions=[
                a.value
                for a in actions
                if a not in (ActionType.LIKED, ActionType.REPLIED)
            ],
        )

    def has_action(
        self, user_id: str, post_id: str, action_type: ActionType
    ) -> bool:
        """Check if a user has performed a specific action on a post.

        Accessing an entry marks it as most-recently-used.

        Args:
            user_id: The user to check.
            post_id: The post to check.
            action_type: The action type to look for.

        Returns:
            True if the user has performed the action, False otherwise.
        """
        key = (user_id, post_id)
        if key not in self._store:
            return False
        self._store.move_to_end(key)
        return action_type in self._store[key]

    def _evict_if_needed(self) -> None:
        """Evict the least recently used entry if at capacity."""
        if len(self._store) >= self._capacity:
            self._store.popitem(last=False)
