"""URL Frontier with priority front queues and politeness back queues."""

import heapq
import time
from collections import deque
from typing import Optional

from web_crawler.models import FrontierEntry


class URLFrontier:
    """URL Frontier with priority front queues and politeness back queues.

    Front queues: A priority heap (heapq) that determines crawl order.
    Higher priority URLs (lower enum value) are dequeued first.

    Back queues: A dict mapping host -> deque of FrontierEntry.
    Enforces per-host delay between consecutive requests.
    """

    def __init__(self, per_host_delay: float = 1.0) -> None:
        self._per_host_delay = per_host_delay
        # Front queue: min-heap ordered by (priority_value, counter, entry)
        self._front_queue: list[tuple[int, int, FrontierEntry]] = []
        self._counter = 0  # Tie-breaker for heap stability
        # Back queues: host -> deque of entries waiting for politeness
        self._back_queues: dict[str, deque[FrontierEntry]] = {}
        # Per-host last access timestamps (monotonic)
        self._host_last_access: dict[str, float] = {}
        self._size = 0

    def put(self, entry: FrontierEntry) -> None:
        """Add a URL to the frontier.

        Assigns to front queue by priority and back queue by host.
        """
        # Add to back queue for the host
        if entry.host not in self._back_queues:
            self._back_queues[entry.host] = deque()
        self._back_queues[entry.host].append(entry)

        # Add to front queue (priority heap)
        heapq.heappush(
            self._front_queue,
            (entry.priority.value, self._counter, entry),
        )
        self._counter += 1
        self._size += 1

    def get(self) -> Optional[FrontierEntry]:
        """Dequeue the next URL respecting priority and politeness.

        Returns the highest-priority URL whose host's politeness delay
        has elapsed. Returns None if no URL is currently available.
        """
        now = time.monotonic()
        skipped: list[tuple[int, int, FrontierEntry]] = []

        result: Optional[FrontierEntry] = None
        while self._front_queue:
            priority_val, counter, entry = heapq.heappop(self._front_queue)

            # Check politeness: has enough time passed for this host?
            last_access = self._host_last_access.get(entry.host, 0.0)
            if now - last_access >= self._per_host_delay:
                # This entry is ready
                result = entry
                self._host_last_access[entry.host] = now
                # Remove from back queue
                if entry.host in self._back_queues:
                    back_q = self._back_queues[entry.host]
                    if back_q and back_q[0] is entry:
                        back_q.popleft()
                    if not back_q:
                        del self._back_queues[entry.host]
                self._size -= 1
                break
            else:
                # Not ready yet, skip for now
                skipped.append((priority_val, counter, entry))

        # Put skipped entries back
        for item in skipped:
            heapq.heappush(self._front_queue, item)

        return result

    def is_empty(self) -> bool:
        """Check if the frontier has no pending URLs."""
        return self._size == 0

    @property
    def size(self) -> int:
        """Number of pending URLs in the frontier."""
        return self._size
