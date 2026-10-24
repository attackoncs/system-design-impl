"""Thread-safe process-local counters with nonnegative values."""

import threading

from news_feed_system.models import CounterType


class CounterCache:
    """Atomic within this process; distributed replicas need shared storage."""

    def __init__(self) -> None:
        self._counters: dict[tuple[str, CounterType], int] = {}
        self._lock = threading.RLock()

    def increment(self, entity_id: str, counter_type: CounterType) -> int:
        with self._lock:
            key = (entity_id, counter_type)
            self._counters[key] = self._counters.get(key, 0) + 1
            return self._counters[key]

    def decrement(self, entity_id: str, counter_type: CounterType) -> int:
        with self._lock:
            key = (entity_id, counter_type)
            self._counters[key] = max(0, self._counters.get(key, 0) - 1)
            return self._counters[key]

    def get(self, entity_id: str, counter_type: CounterType) -> int:
        with self._lock:
            return self._counters.get((entity_id, counter_type), 0)

    def get_batch(
        self, keys: list[tuple[str, CounterType]]
    ) -> dict[tuple[str, CounterType], int]:
        with self._lock:
            return {key: self._counters.get(key, 0) for key in keys}
