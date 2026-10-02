"""Fanout workers for async news feed distribution.

FanoutWorker consumes FanoutTask items from a MessageQueue and appends
FeedEntry records to each target user's NewsFeedCache. WorkerPool manages
a configurable number of concurrent worker asyncio tasks with graceful
shutdown support.
"""

from __future__ import annotations

import asyncio
import logging
from typing import List, Optional

from news_feed_system.models import FanoutTask, FeedEntry
from news_feed_system.news_feed_cache import NewsFeedCache
from news_feed_system.queue import MessageQueue

logger = logging.getLogger(__name__)

# Sentinel object used to signal workers to stop.
_STOP_SENTINEL = object()


class FanoutWorker:
    """Async worker that consumes FanoutTask items and writes to NewsFeedCache.

    Each worker runs as an asyncio task, continuously dequeuing FanoutTask
    items and appending a FeedEntry to every target user's news feed cache.
    Workers stop cleanly when a sentinel value is received.

    Args:
        queue: The MessageQueue to consume FanoutTask items from.
        cache: The NewsFeedCache to write FeedEntry items into.
        worker_id: Optional identifier for logging purposes.
    """

    def __init__(
        self,
        queue: MessageQueue,
        cache: NewsFeedCache,
        worker_id: int = 0,
    ) -> None:
        self._queue = queue
        self._cache = cache
        self._worker_id = worker_id
        self._task: Optional[asyncio.Task] = None  # type: ignore[type-arg]

    async def _run(self, sentinel_queue: asyncio.Queue) -> None:  # type: ignore[type-arg]
        """Main worker loop.

        Dequeues items from the shared sentinel_queue (which wraps the
        MessageQueue) and processes each FanoutTask until the sentinel is
        received.

        Args:
            sentinel_queue: An asyncio.Queue that yields either FanoutTask
                            items or the _STOP_SENTINEL object.
        """
        while True:
            item = await sentinel_queue.get()
            try:
                if item is _STOP_SENTINEL:
                    # Put the sentinel back so other workers can also stop.
                    await sentinel_queue.put(_STOP_SENTINEL)
                    logger.debug("Worker %d received stop sentinel", self._worker_id)
                    return

                task: FanoutTask = item
                entry = FeedEntry(
                    post_id=task.post_id,
                    author_id=task.author_id,
                    timestamp=task.timestamp,
                )
                for user_id in task.target_user_ids:
                    self._cache.append(user_id, entry)

                logger.debug(
                    "Worker %d processed task post_id=%s for %d users",
                    self._worker_id,
                    task.post_id,
                    len(task.target_user_ids),
                )
            finally:
                sentinel_queue.task_done()

    def start(self, sentinel_queue: asyncio.Queue) -> asyncio.Task:  # type: ignore[type-arg]
        """Start the worker as an asyncio task.

        Args:
            sentinel_queue: The shared queue (with sentinel support) to
                            consume from.

        Returns:
            The asyncio.Task running this worker.
        """
        self._task = asyncio.create_task(self._run(sentinel_queue))
        return self._task

    @property
    def task(self) -> Optional[asyncio.Task]:  # type: ignore[type-arg]
        """The underlying asyncio.Task, or None if not started."""
        return self._task


class WorkerPool:
    """Manages a pool of FanoutWorker asyncio tasks.

    Provides start/stop lifecycle management with graceful shutdown: on stop,
    a sentinel is enqueued so all workers drain in-flight tasks before
    terminating.

    Args:
        queue: The MessageQueue that workers consume from.
        cache: The NewsFeedCache that workers write to.
        num_workers: Number of concurrent worker tasks to run.
    """

    def __init__(
        self,
        queue: MessageQueue,
        cache: NewsFeedCache,
        num_workers: int = 4,
    ) -> None:
        if num_workers <= 0:
            raise ValueError(f"num_workers must be positive, got {num_workers}")
        self._queue = queue
        self._cache = cache
        self._num_workers = num_workers
        self._workers: List[FanoutWorker] = []
        # Internal asyncio.Queue that wraps the MessageQueue items and also
        # accepts the sentinel for shutdown signalling.
        self._sentinel_queue: asyncio.Queue = asyncio.Queue()  # type: ignore[type-arg]
        self._running = False
        self._pump_task: Optional[asyncio.Task] = None  # type: ignore[type-arg]

    async def _pump(self) -> None:
        """Pump items from MessageQueue into the internal sentinel_queue.

        Runs until cancelled (on stop). This decouples the MessageQueue
        (which only holds FanoutTask) from the sentinel mechanism.
        """
        while True:
            try:
                item = await self._queue.dequeue()
                await self._sentinel_queue.put(item)
            except asyncio.CancelledError:
                return

    async def start(self) -> None:
        """Start the worker pool.

        Creates and starts ``num_workers`` FanoutWorker asyncio tasks and
        a pump task that forwards items from the MessageQueue into the
        internal sentinel queue.

        Raises:
            RuntimeError: If the pool is already running.
        """
        if self._running:
            raise RuntimeError("WorkerPool is already running")

        self._running = True
        self._workers = [
            FanoutWorker(self._queue, self._cache, worker_id=i)
            for i in range(self._num_workers)
        ]

        # Start the pump that forwards MessageQueue items to sentinel_queue.
        self._pump_task = asyncio.create_task(self._pump())

        # Start all workers.
        for worker in self._workers:
            worker.start(self._sentinel_queue)

        logger.debug("WorkerPool started with %d workers", self._num_workers)

    async def stop(self) -> None:
        """Stop the worker pool gracefully.

        Cancels the pump task (no new items will be forwarded), then enqueues
        a single sentinel value. Workers pass the sentinel along so every
        worker receives it and exits after finishing any in-flight task.
        Finally, waits for all worker tasks to complete.

        Raises:
            RuntimeError: If the pool is not running.
        """
        if not self._running:
            raise RuntimeError("WorkerPool is not running")

        # Stop the pump so no new items are forwarded.
        if self._pump_task is not None:
            self._pump_task.cancel()
            try:
                await self._pump_task
            except asyncio.CancelledError:
                pass
            self._pump_task = None

        # Enqueue the sentinel once; each worker re-enqueues it for the next.
        await self._sentinel_queue.put(_STOP_SENTINEL)

        # Wait for all workers to finish.
        worker_tasks = [w.task for w in self._workers if w.task is not None]
        if worker_tasks:
            await asyncio.gather(*worker_tasks, return_exceptions=True)

        self._running = False
        self._workers = []
        logger.debug("WorkerPool stopped")

    @property
    def is_running(self) -> bool:
        """True if the pool has been started and not yet stopped."""
        return self._running

    @property
    def num_workers(self) -> int:
        """The configured number of worker tasks."""
        return self._num_workers
