"""Thread-safe bounded LRU of per-user, per-post actions."""

import collections
import threading

from news_feed_system.models import ActionState, ActionType


class ActionCache:
    def __init__(self, capacity: int = 10000) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity <= 0:
            raise ValueError("capacity must be a positive integer")
        self._capacity = capacity
        self._store: collections.OrderedDict[tuple[str, str], set[ActionType]] = (
            collections.OrderedDict()
        )
        self._lock = threading.RLock()

    @property
    def capacity(self) -> int:
        return self._capacity

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)

    def record_action(
        self, user_id: str, post_id: str, action_type: ActionType
    ) -> None:
        with self._lock:
            key = (user_id, post_id)
            self._store.setdefault(key, set()).add(action_type)
            self._store.move_to_end(key)
            if len(self._store) > self._capacity:
                self._store.popitem(last=False)

    def remove_action(
        self, user_id: str, post_id: str, action_type: ActionType
    ) -> None:
        with self._lock:
            key = (user_id, post_id)
            actions = self._store.get(key)
            if actions is None:
                return
            actions.discard(action_type)
            if not actions:
                del self._store[key]
            else:
                self._store.move_to_end(key)

    def get_actions(self, user_id: str, post_id: str) -> ActionState:
        with self._lock:
            key = (user_id, post_id)
            actions = self._store.get(key)
            if actions is None:
                return ActionState()
            self._store.move_to_end(key)
            return ActionState(
                liked=ActionType.LIKED in actions,
                replied=ActionType.REPLIED in actions,
                other_actions=sorted(
                    action.value
                    for action in actions
                    if action not in (ActionType.LIKED, ActionType.REPLIED)
                ),
            )

    def has_action(self, user_id: str, post_id: str, action_type: ActionType) -> bool:
        with self._lock:
            key = (user_id, post_id)
            actions = self._store.get(key)
            if actions is None:
                return False
            self._store.move_to_end(key)
            return action_type in actions
