"""URL Frontier with priority front queues and politeness back queues."""

import heapq
import time
from typing import Optional

import web_crawler.models


class URLFrontier:
    """URL Frontier with priority front queues and politeness back queues.

    Per-host priority heaps preserve URL priority and FIFO tie order. Only
    eligible host heads enter the ready heap; delayed hosts use a wake-up heap.
    A large backlog on a reserved host is never scanned on each poll.

    Methods are synchronous and confined to the crawler's event-loop thread.
    """

    def __init__(self, per_host_delay: float = 1.0) -> None:
        self._per_host_delay = per_host_delay
        # Ready host heads, ordered by (priority, global enqueue order, entry).
        self._front_queue: list[tuple[int, int, web_crawler.models.FrontierEntry]] = []
        self._counter = 0
        self._back_queues: dict[str, list] = {}
        self._delayed: list[tuple[float, str]] = []
        self._waiting: dict[str, float] = {}
        # Per-host last access timestamps (monotonic)
        self._host_last_access: dict[str, float] = {}
        self._size = 0
        self._active_hosts = set()

    def put(self, entry: web_crawler.models.FrontierEntry) -> None:
        """Queue a URL; publish a new head only if the host is eligible."""
        queue = self._back_queues.setdefault(entry.host, [])
        item = (entry.priority.value, self._counter, entry)
        heapq.heappush(queue, item)
        self._counter += 1
        self._size += 1
        if queue[0] is item:
            self._schedule(entry.host, time.monotonic())

    def _schedule(self, host: str, now: float) -> None:
        if host in self._active_hosts or host in self._waiting:
            return
        queue = self._back_queues.get(host)
        if not queue:
            return
        last_access = self._host_last_access.get(host)
        due = now if last_access is None else last_access + self._per_host_delay
        if due > now:
            self._waiting[host] = due
            heapq.heappush(self._delayed, (due, host))
        else:
            heapq.heappush(self._front_queue, queue[0])

    def get(self, *, reserve=False) -> Optional[web_crawler.models.FrontierEntry]:
        """Dequeue the next URL respecting priority and politeness.

        Returns the highest-priority URL whose host's politeness delay has
        elapsed. Returns None if no URL is currently available.
        """
        now = time.monotonic()
        while self._delayed and self._delayed[0][0] <= now:
            due, host = heapq.heappop(self._delayed)
            if self._waiting.get(host) == due:
                del self._waiting[host]
                self._schedule(host, now)
        while self._front_queue:
            _, counter, entry = heapq.heappop(self._front_queue)
            queue = self._back_queues.get(entry.host)
            # A later, higher-priority enqueue may supersede a published head.
            if (
                not queue
                or queue[0][1] != counter
                or entry.host in self._active_hosts
                or entry.host in self._waiting
            ):
                continue
            heapq.heappop(queue)
            if not queue:
                del self._back_queues[entry.host]
            self._size -= 1
            self._host_last_access[entry.host] = now
            if reserve:
                self._active_hosts.add(entry.host)
            else:
                self._schedule(entry.host, now)
            return entry
        return None

    def complete(self, entry):
        """Release a reserved host and start its post-request delay."""
        self._active_hosts.discard(entry.host)
        now = time.monotonic()
        self._host_last_access[entry.host] = now
        self._waiting.pop(entry.host, None)
        self._schedule(entry.host, now)

    def is_empty(self) -> bool:
        """Check if the frontier has no pending URLs."""
        return self._size == 0

    @property
    def size(self) -> int:
        """Number of pending URLs in the frontier."""
        return self._size
